"""Auth, access control, secrets handling."""

import time

import jwt
from fastapi.routing import APIRoute

from backend.auth.security import ACCESS_TOKEN_SECRET, ALGORITHM
from backend.main import PUBLIC_API, app
from tests.conftest import OWNER


def test_every_private_api_route_requires_auth(client):
    for route in app.routes:
        if not isinstance(route, APIRoute) or not route.path.startswith("/api/") or route.path in PUBLIC_API:
            continue
        path = route.path.replace("{plan_id}", "1").replace("{alert_id}", "1").replace("{action}", "resolve") \
            .replace("{rule_id}", "1").replace("{log_id}", "1").replace("{creative_id}", "1").replace("{launch_id}", "1")
        for method in route.methods - {"HEAD", "OPTIONS"}:
            r = client.request(method, path)
            assert r.status_code == 401, f"{method} {path} returned {r.status_code} without a token"


def test_tampered_and_expired_tokens_rejected(client, auth):
    good = auth["Authorization"].split()[1]
    head, body, sig = good.split(".")
    bad = f"{head}.{body}.{'A' * len(sig)}"
    assert client.get("/api/users/me", headers={"Authorization": f"Bearer {bad}"}).status_code == 401
    expired = jwt.encode({"sub": OWNER["email"], "exp": int(time.time()) - 10, "token_type": "access"},
                         ACCESS_TOKEN_SECRET, algorithm=ALGORITHM)
    assert client.get("/api/users/me", headers={"Authorization": f"Bearer {expired}"}).status_code == 401
    forged = jwt.encode({"sub": OWNER["email"], "exp": int(time.time()) + 600, "token_type": "access"},
                        "not-the-secret", algorithm=ALGORITHM)
    assert client.get("/api/users/me", headers={"Authorization": f"Bearer {forged}"}).status_code == 401
    none_alg = jwt.encode({"sub": OWNER["email"], "token_type": "access"}, key=None, algorithm="none")
    assert client.get("/api/users/me", headers={"Authorization": f"Bearer {none_alg}"}).status_code == 401


def test_short_password_rejected(client, auth):
    r = client.post("/api/users/register", json={"email": "new@example.com", "password": "short"}, headers=auth)
    assert r.status_code == 422


def test_owner_can_add_user_non_owner_cannot(client, auth):
    r = client.post("/api/users/register", json={"email": "staff@example.com", "password": "StaffPass123"}, headers=auth)
    assert r.status_code in (201, 400)  # 400 if a previous run already added it
    staff = client.post("/api/auth/login", json={"email": "staff@example.com", "password": "StaffPass123"}).json()
    h = {"Authorization": f"Bearer {staff['access_token']}"}
    assert client.get("/api/users/me", headers=h).json()["is_superuser"] is False
    assert client.post("/api/users/register", json={"email": "x2@example.com", "password": "Whatever123"}, headers=h).status_code == 403


def test_meta_token_is_masked(client, auth):
    client.post("/api/meta/settings", json={"values": {"meta_access_token": "EAAsecret"}}, headers=auth)
    s = client.get("/api/meta/settings", headers=auth).json()["settings"]
    assert s["meta_access_token"] == "********"
    # posting the mask back must not overwrite the real token
    client.post("/api/meta/settings", json={"values": {"meta_access_token": "********"}}, headers=auth)
    from backend import settings_store
    from backend.db import get_session
    with get_session() as sess:
        assert settings_store.get(sess, "meta_access_token") == "EAAsecret"
        settings_store.set_many(sess, {"meta_access_token": ""})


def test_media_delete_cannot_escape_media_dir(tmp_path):
    from backend import creatives
    from backend.config import DATA_DIR
    victim = DATA_DIR / "victim.txt"
    victim.write_text("keep me")
    creatives.delete_file("../victim.txt")
    assert victim.exists()
    victim.unlink()


# ---------- behaviour that must hold ----------

def test_settings_endpoint_rejects_internal_keys(client, auth):
    """The Meta form must not be able to set internal keys like product_photo (path into the media dir)."""
    client.post("/api/meta/settings", json={"values": {"product_photo": "../app.db", "auto_actions": '{"no_purchases": true}'}},
                headers=auth)
    from backend import settings_store
    from backend.db import get_session
    with get_session() as sess:
        assert settings_store.get(sess, "product_photo") != "../app.db"
        assert settings_store.get(sess, "auto_actions") != '{"no_purchases": true}'


def test_oauth_start_requires_login(client):
    """Anyone could otherwise start OAuth and replace the app's Meta token with their own account's."""
    assert "/api/meta/oauth/start" not in PUBLIC_API


def test_logout_revokes_refresh_token(client, auth):
    tok = client.post("/api/auth/login", json=OWNER).json()
    client.post("/api/auth/logout", json={"refresh_token": tok["refresh_token"]},
                headers={"Authorization": f"Bearer {tok['access_token']}"})
    r = client.post("/api/auth/refresh", json={"refresh_token": tok["refresh_token"]})
    assert r.status_code == 401, "refresh token still works after logout"


def test_refresh_token_is_single_use(client, auth):
    tok = client.post("/api/auth/login", json=OWNER).json()
    first = client.post("/api/auth/refresh", json={"refresh_token": tok["refresh_token"]})
    assert first.status_code == 200
    second = client.post("/api/auth/refresh", json={"refresh_token": tok["refresh_token"]})
    assert second.status_code == 401, "an already-rotated refresh token can be reused"



def test_no_public_signup_even_on_empty_database(client, monkeypatch):
    """With zero users the website must still not let anyone create an account."""
    import backend.auth.routes as R
    monkeypatch.setattr(R, "_user_count", lambda: 0)
    assert client.get("/api/auth/setup-status").json() == {"has_users": False}
    r = client.post("/api/users/register", json={"email": "stranger@example.com", "password": "Password123"})
    assert r.status_code == 403


def test_create_owner_command(monkeypatch):
    from scripts import create_owner
    from backend.auth.security import verify_password
    from backend.db import User, get_session
    answers = iter(["Boss@Example.com"])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    monkeypatch.setattr(create_owner.getpass, "getpass", lambda *a: "OwnerPass123")
    create_owner.main()
    with get_session() as s:
        u = s.query(User).filter(User.email == "boss@example.com").one()
        assert u.is_superuser and verify_password("OwnerPass123", u.hashed_password)
    # running it again resets the password instead of failing
    monkeypatch.setattr("builtins.input", lambda *a: "boss@example.com")
    monkeypatch.setattr(create_owner.getpass, "getpass", lambda *a: "NewPass12345")
    create_owner.main()
    with get_session() as s:
        assert verify_password("NewPass12345", s.query(User).filter(User.email == "boss@example.com").one().hashed_password)
