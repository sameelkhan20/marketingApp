"""Gmail API sign-in from inside the app (same OAuth as the Social Agent's set-gmail, as a Connect button).

Needs GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET in .env: a Google Cloud OAuth client of type "Desktop app"
with the Gmail API enabled (the Social Agent's client works). The browser lands on
http://127.0.0.1:8765/?code=... (the page does not load); the owner pastes that address back. The
refresh token and the sending address are stored encrypted (secrets_store)."""

import base64
import hashlib
import json
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from . import secrets_store
from .config import GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET

REDIRECT = "http://127.0.0.1:8765/"
SCOPES = "https://www.googleapis.com/auth/gmail.send openid email"
STATE_TTL = 15 * 60
_states: dict[str, dict] = {}


class GmailAuthError(RuntimeError):
    pass


def client_configured() -> bool:
    return bool(GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET)


def start_login(user_id: int) -> str:
    if not client_configured():
        raise GmailAuthError("Set GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET in .env first (a Google OAuth "
                             "client of type Desktop app; the Social Agent's works), then restart.")
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(16)
    now = time.time()
    for k in [k for k, v in _states.items() if v["at"] < now - STATE_TTL]:
        _states.pop(k, None)
    _states[state] = {"verifier": verifier, "user": user_id, "at": now}
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
        "client_id": GMAIL_CLIENT_ID, "redirect_uri": REDIRECT, "response_type": "code", "scope": SCOPES,
        "access_type": "offline", "prompt": "consent", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256"})


def complete_login(pasted_url: str, user_id: int) -> str:
    q = parse_qs(urlparse((pasted_url or "").strip()).query)
    code, state, error = ((q.get(k) or [""])[0] for k in ("code", "state", "error"))
    if error:
        raise GmailAuthError(f"Google says: {error}. Click Connect again.")
    if not code or not state:
        raise GmailAuthError("That is not the address from the Google sign-in: copy the whole address "
                             "(it starts with http://127.0.0.1:8765/?state=...).")
    started = _states.pop(state, None)
    if not started or started["at"] < time.time() - STATE_TTL:
        raise GmailAuthError("This sign-in has expired or was used. Click Connect again.")
    if started["user"] != user_id:
        raise GmailAuthError("This sign-in was started by someone else.")
    r = requests.post("https://oauth2.googleapis.com/token", data={
        "code": code, "client_id": GMAIL_CLIENT_ID, "client_secret": GMAIL_CLIENT_SECRET, "redirect_uri": REDIRECT,
        "grant_type": "authorization_code", "code_verifier": started["verifier"]}, timeout=30)
    if r.status_code >= 400:
        raise GmailAuthError(f"Google refused the sign-in: {r.text[:200]}")
    tok = r.json()
    if not tok.get("refresh_token"):
        raise GmailAuthError("Google gave no refresh token. Remove the app's access at "
                             "myaccount.google.com/permissions and connect again.")
    try:
        p = tok["id_token"].split(".")[1]
        email = json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4))).get("email", "")
    except (KeyError, IndexError, ValueError):
        email = ""
    if not email:
        raise GmailAuthError("Could not read the Google account's address.")
    secrets_store.put({"gmail_refresh_token": tok["refresh_token"], "gmail_sender": email})
    return email


def disconnect() -> None:
    secrets_store.delete("gmail_refresh_token", "gmail_sender")
