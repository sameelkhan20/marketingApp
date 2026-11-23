"""Higgsfield (Social Agent method) and the in-app Gmail connect. No network: requests is faked."""

import base64
import json
from urllib.parse import parse_qs, urlparse

import pytest

from backend import gmail_auth, higgsfield as hf, imagegen, secrets_store


class R:
    def __init__(self, status=200, data=None, headers=None, body=b""):
        self.status_code, self._d = status, data if data is not None else {}
        self.headers = headers or {"content-type": "application/json"}
        self.text = json.dumps(self._d)
        self.content = body or self.text.encode()

    def json(self):
        return self._d

    def iter_content(self, n):
        yield self.content

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(imagegen, "IMAGE_PROVIDER", "higgsfield")
    hf.disconnect()
    gmail_auth.disconnect()
    yield
    hf.disconnect()
    gmail_auth.disconnect()


def _login(monkeypatch):
    monkeypatch.setattr(hf.requests, "post", lambda url, **k: R(200, {"client_id": "cid"}) if url == hf.REGISTER_URL
                        else R(200, {"access_token": "at", "refresh_token": "rt", "expires_in": 3600}))
    q = parse_qs(urlparse(hf.start_login(1)).query)
    assert q["redirect_uri"] == [hf.REDIRECT] and q["code_challenge_method"] == ["S256"]
    return q["state"][0]


def test_higgsfield_connect_flow(monkeypatch):
    state = _login(monkeypatch)
    with pytest.raises(hf.HiggsfieldError, match="someone else"):
        hf.complete_login(f"http://localhost:8765/callback?code=c&state={state}", user_id=2)
    state = _login(monkeypatch)
    hf.complete_login(f"http://localhost:8765/callback?code=c&state={state}", user_id=1)
    assert hf.status()["connected"] and secrets_store.get("hf_refresh_token") == "rt"
    with pytest.raises(hf.HiggsfieldError, match="expired or was used"):  # one use only
        hf.complete_login(f"http://localhost:8765/callback?code=c&state={state}", user_id=1)
    from backend.db import Setting, get_session
    with get_session() as s:  # stored encrypted
        assert s.get(Setting, "secret:hf_refresh_token").value != "rt"


class FakeSession:
    """Plays Higgsfield's MCP server: import -> generate -> jobs_wait -> download."""

    def __init__(self, generate_status=200):
        self.calls, self.generate_status = [], generate_status

    def post(self, url, headers=None, json=None, timeout=None):
        if json.get("method") == "tools/call":
            name = json["params"]["name"]
            self.calls.append((name, json["params"]["arguments"]))
            if name == "generate_image" and self.generate_status != 200:
                return R(self.generate_status)
            sc = {"media_import_url": {"media_id": "m1"}, "generate_image": {"job_id": "j1"},
                  "jobs_wait": {"jobs": [{"status": "completed", "url": "https://cdn.hf/x.png"}]}}[name]
            return R(200, {"jsonrpc": "2.0", "id": json["id"], "result": {"structuredContent": sc}},
                     headers={"content-type": "application/json", "mcp-session-id": "s1"})
        return R(200, {"jsonrpc": "2.0", "id": json.get("id"), "result": {}})

    def get(self, url, timeout=None, stream=False):
        return R(200, body=b"PNGDATA")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def _connected():
    secrets_store.put({"hf_refresh_token": "rt", "hf_access_token": "at", "hf_expires_at": "9999999999"})


def test_make_picture_with_reference(monkeypatch):
    _connected()
    fake = FakeSession()
    monkeypatch.setattr(hf.requests, "Session", lambda: fake)
    assert hf.make_picture(["https://r2/ref.png"], "jacket", "4:5") == b"PNGDATA"
    assert [c[0] for c in fake.calls] == ["media_import_url", "generate_image", "jobs_wait"]
    params = fake.calls[1][1]["params"]
    assert params["medias"] == [{"role": "image_references", "value": "m1"}] and params["aspect_ratio"] == "4:5"
    assert params["use_unlim"] is False and params["count"] == 1


def test_generate_not_resent_after_502(monkeypatch):
    """502 after submit: the job may exist and be billed, so it must not be sent again."""
    _connected()
    fake = FakeSession(generate_status=502)
    monkeypatch.setattr(hf.requests, "Session", lambda: fake)
    monkeypatch.setattr(hf.time, "sleep", lambda s: None)
    with pytest.raises(hf.HiggsfieldError) as ex:
        hf.make_picture([], "jacket", "1:1")
    assert ex.value.charged and [c[0] for c in fake.calls].count("generate_image") == 1


def test_reference_needs_r2(monkeypatch):
    monkeypatch.setattr("backend.r2.configured", lambda: False)
    with pytest.raises(hf.HiggsfieldError, match="R2"):
        hf.generate_with_reference("p", (b"x", "image/png"), "1:1")


def test_imagegen_uses_higgsfield(monkeypatch, tmp_path):
    secrets_store.put({"hf_refresh_token": "rt"})
    got = {}
    monkeypatch.setattr(hf, "generate_with_reference",
                        lambda prompt, ref, aspect: got.update(prompt=prompt, ref=ref, aspect=aspect) or b"IMG")
    ref = tmp_path / "p.png"
    ref.write_bytes(b"photo")
    assert imagegen.status()["provider"] == "higgsfield"
    assert imagegen.generate("street shot", reference=str(ref), aspect="9:16") == b"IMG"
    assert got["ref"] == (b"photo", "image/png") and "garment" in got["prompt"] and "no logos" in got["prompt"]


def test_imagegen_off_when_not_connected():
    assert imagegen.status()["configured"] is False
    with pytest.raises(imagegen.ImageGenError, match="Higgsfield is not connected"):
        imagegen.generate("x")


def test_gmail_connect_in_app(monkeypatch):
    monkeypatch.setattr(gmail_auth, "GMAIL_CLIENT_ID", "id.apps.googleusercontent.com")
    monkeypatch.setattr(gmail_auth, "GMAIL_CLIENT_SECRET", "secret-123456")
    payload = base64.urlsafe_b64encode(json.dumps({"email": "sender@gmail.com"}).encode()).decode().rstrip("=")
    monkeypatch.setattr(gmail_auth.requests, "post",
                        lambda url, **k: R(200, {"refresh_token": "grt", "id_token": f"x.{payload}.y"}))
    state = parse_qs(urlparse(gmail_auth.start_login(1)).query)["state"][0]
    assert gmail_auth.complete_login(f"http://127.0.0.1:8765/?state={state}&code=abc", 1) == "sender@gmail.com"
    from backend import notify
    monkeypatch.setattr(notify, "GMAIL_CLIENT_ID", "id")
    monkeypatch.setattr(notify, "GMAIL_CLIENT_SECRET", "s")
    monkeypatch.setattr(notify, "ALERT_EMAIL", "me@example.com")
    assert notify._gmail_creds() == ("grt", "sender@gmail.com") and notify.gmail_configured()


def test_connect_routes_need_login(client):
    for path in ("/api/higgsfield/connect", "/api/gmail/connect", "/api/higgsfield/complete"):
        assert client.post(path).status_code == 401


def test_gmail_connect_without_client(client, auth):
    r = client.post("/api/gmail/connect", headers=auth)
    assert r.status_code == 400 and "GMAIL_CLIENT_ID" in r.json()["detail"]
