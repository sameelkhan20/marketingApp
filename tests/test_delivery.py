"""Gemini images and Gmail -> Brevo email, same methods as the Social Agent (no network)."""

import base64
import json

import pytest

from backend import imagegen, notify
from tests.conftest import PNG_1x1


class _R:
    def __init__(self, status, data):
        self.status_code, self._d = status, data
        self.text = json.dumps(data)
        self.content = self.text.encode()

    def json(self):
        return self._d


def test_gemini_sends_product_photo_and_aspect(monkeypatch, tmp_path):
    ref = tmp_path / "jacket.png"
    ref.write_bytes(PNG_1x1)
    sent = {}

    def post(url, headers=None, json=None, timeout=None, **k):
        sent.update(url=url, headers=headers, body=json)
        return _R(200, {"candidates": [{"content": {"parts": [{"inlineData": {"data": base64.b64encode(b"IMG").decode()}}]}}]})

    monkeypatch.setattr(imagegen, "IMAGE_PROVIDER", "gemini")
    monkeypatch.setattr(imagegen, "IMAGE_API_KEY", "k")
    monkeypatch.setattr(imagegen.requests, "post", post)
    assert imagegen.generate("Model in a street", reference=str(ref), aspect="9:16") == b"IMG"
    assert sent["url"].endswith(":generateContent") and sent["headers"] == {"x-goog-api-key": "k"}
    parts = sent["body"]["contents"][0]["parts"]
    assert parts[0]["inline_data"]["mime_type"] == "image/png"          # product photo first
    assert "no logos" in parts[1]["text"] and "Show exactly this product" in parts[1]["text"]
    assert sent["body"]["generationConfig"]["imageConfig"]["aspectRatio"] == "9:16"


def test_gemini_block_reason_surfaces(monkeypatch):
    monkeypatch.setattr(imagegen, "IMAGE_PROVIDER", "gemini")
    monkeypatch.setattr(imagegen, "IMAGE_API_KEY", "k")
    monkeypatch.setattr(imagegen.requests, "post", lambda *a, **k: _R(200, {"promptFeedback": {"blockReason": "SAFETY"}}))
    with pytest.raises(imagegen.ImageGenError, match="SAFETY"):
        imagegen.generate("x")


def test_image_off_without_key(monkeypatch):
    monkeypatch.setattr(imagegen, "IMAGE_PROVIDER", "gemini")
    monkeypatch.setattr(imagegen, "IMAGE_API_KEY", "")
    assert imagegen.status()["configured"] is False
    with pytest.raises(imagegen.ImageGenError):
        imagegen.generate("x")


@pytest.fixture
def mail(monkeypatch):
    for k, v in {"ALERT_EMAIL": "a@example.com; b@example.com", "GMAIL_CLIENT_ID": "id", "GMAIL_CLIENT_SECRET": "s",
                 "GMAIL_REFRESH_TOKEN": "r", "GMAIL_SENDER": "me@gmail.com", "BREVO_API_KEY": "bk",
                 "ALERT_FROM": "alerts@shop.com"}.items():
        monkeypatch.setattr(notify, k, v)
    notify._gmail_token.update(value="", until=0)
    state = {"calls": [], "gmail_status": 200}

    def post(url, **k):
        state["calls"].append((url, k))
        if url == notify.GOOGLE_TOKEN_URL:
            return _R(200, {"access_token": "at", "expires_in": 3600})
        return _R(state["gmail_status"], {}) if url == notify.GMAIL_SEND_URL else _R(201, {})

    monkeypatch.setattr(notify.requests, "post", post)
    return state


def test_gmail_first(mail):
    assert notify.status()["provider"] == "gmail + brevo"
    assert notify.send_email("Subj", "<p>h</p>", "t") == "gmail"
    send = next(k for u, k in mail["calls"] if u == notify.GMAIL_SEND_URL)
    assert send["headers"]["Authorization"] == "Bearer at"
    raw = base64.urlsafe_b64decode(send["json"]["raw"]).decode()
    assert "To: a@example.com, b@example.com" in raw and "Subject: Subj" in raw
    assert not any(u == notify.BREVO_URL for u, _ in mail["calls"])


def test_brevo_when_gmail_refuses(mail):
    mail["gmail_status"] = 403
    assert notify.send_email("Subj", "<p>h</p>", "t") == "brevo"
    brevo = next(k for u, k in mail["calls"] if u == notify.BREVO_URL)
    assert brevo["headers"]["api-key"] == "bk" and len(brevo["json"]["to"]) == 2


def test_both_fail_raises_with_reasons(mail, monkeypatch):
    mail["gmail_status"] = 500
    monkeypatch.setattr(notify, "_send_brevo", lambda *a: "Brevo 401: bad key")
    with pytest.raises(RuntimeError, match="Gmail 500.*Brevo 401"):
        notify.send_email("s", "h", "t")
