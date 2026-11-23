"""Account page: password change, team, plan delete and backups."""

from backend import backup
from backend.db import Launch, get_session


def _login(client, email, password):
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    return r.status_code, ({"Authorization": f"Bearer {r.json()['access_token']}"} if r.status_code == 200 else None)


def test_team_and_password(client, auth):
    r = client.post("/api/users/register", json={"email": "helper@example.com", "password": "HelperPass1"}, headers=auth)
    assert r.status_code in (201, 400)  # 400 when an earlier run added it
    code, h = _login(client, "helper@example.com", "HelperPass1")
    assert code == 200
    # a team member cannot manage the team or backups
    assert client.get("/api/users", headers=h).status_code == 403
    assert client.get("/api/backups", headers=h).status_code == 403
    # password change needs the right current password
    bad = client.post("/api/users/me/password", json={"current_password": "nope", "new_password": "NewHelper22"}, headers=h)
    assert bad.status_code == 400
    ok = client.post("/api/users/me/password", json={"current_password": "HelperPass1", "new_password": "NewHelper22"}, headers=h)
    assert ok.status_code == 200
    assert _login(client, "helper@example.com", "HelperPass1")[0] == 401
    assert _login(client, "helper@example.com", "NewHelper22")[0] == 200
    # owner locks them out, then lets them back in
    users = client.get("/api/users", headers=auth).json()
    uid = next(u["id"] for u in users if u["email"] == "helper@example.com")
    owner_id = next(u["id"] for u in users if u["is_superuser"])
    assert client.post(f"/api/users/{owner_id}/active", json={"active": False}, headers=auth).status_code == 400
    assert client.post(f"/api/users/{uid}/active", json={"active": False}, headers=auth).status_code == 200
    assert _login(client, "helper@example.com", "NewHelper22")[0] == 400  # inactive
    assert client.get("/api/today", headers=h).status_code in (400, 401)  # existing token stops working too
    client.post(f"/api/users/{uid}/active", json={"active": True}, headers=auth)
    # put the password back so reruns of this test still work
    _, h = _login(client, "helper@example.com", "NewHelper22")
    client.post("/api/users/me/password", json={"current_password": "NewHelper22", "new_password": "HelperPass1"}, headers=h)


def test_delete_plan_only_before_launch(client, auth, plan):
    with get_session() as s:
        s.add(Launch(plan_id=plan, campaign_id="KEEP", objects={}, status="paused"))
        s.commit()
    assert client.delete(f"/api/plans/{plan}", headers=auth).status_code == 400
    body = {"budget_pkr": 100000, "country": "US", "product": "Throwaway", "niche": "leather_jackets",
            "price_usd": 120, "duration_days": 30}
    pid = client.post("/api/plans", json=body, headers=auth).json()["id"]
    assert client.delete(f"/api/plans/{pid}", headers=auth).status_code == 200
    assert client.get(f"/api/plans/{pid}", headers=auth).status_code == 404


def test_backups(client, auth):
    made = client.post("/api/backups", headers=auth).json()
    assert made["bytes"] > 0
    names = [b["name"] for b in client.get("/api/backups", headers=auth).json()]
    assert made["name"] in names
    r = client.get(f"/api/backups/{made['name']}", headers=auth)
    assert r.status_code == 200 and r.content[:15] == b"SQLite format 3"
    # only names the app wrote can be downloaded
    assert client.get("/api/backups/..%2Fapp.db", headers=auth).status_code == 404
    assert backup.path_of("../app.db") is None and backup.path_of("app.db") is None


def test_backups_keep_the_last_14(monkeypatch, tmp_path):
    monkeypatch.setattr(backup, "DIR", tmp_path)
    for i in range(16):
        (tmp_path / f"app-202601{i + 1:02d}-000000.db").write_bytes(b"x")
    backup.run()
    left = sorted(p.name for p in tmp_path.iterdir())
    assert len(left) == 14 and "app-20260101-000000.db" not in left
