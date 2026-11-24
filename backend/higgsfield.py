"""AI images through the team's Higgsfield plan, ported from the Social Agent (services/higgsfield.py).

  Connect (once, Connections page): dynamic client registration -> authorization code + PKCE. Higgsfield
  only trusts loopback redirects, so the browser lands on http://localhost:8765/callback?code=...
  (the page does not load) and the owner pastes that address into the app (complete_login).
  Tokens are stored encrypted (secrets_store); access tokens refresh themselves (rotation-safe lock).
  One image: media_import_url (references by public https URL, R2) -> generate_image -> jobs_wait
  -> download.

MCP is JSON-RPC over HTTP; an answer comes as JSON or a short text/event-stream. Tokens are never logged.

Billing safety (Social Agent's live lessons): generate_image is resent only on 503 (nothing can have
started); once a job exists, wait/download errors are retried until the time limit rather than
throwing a paid picture away.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from . import secrets_store
from .config import (HIGGSFIELD_ASPECT_RATIO, HIGGSFIELD_BACKGROUND, HIGGSFIELD_MCP_URL, HIGGSFIELD_MODEL,
                     HIGGSFIELD_QUALITY, HIGGSFIELD_RESOLUTION, HIGGSFIELD_VARIANT)

log = logging.getLogger("higgsfield")

AUTH_BASE = "https://mcp.higgsfield.ai"
AUTHORIZE_URL = AUTH_BASE + "/oauth2/authorize"
TOKEN_URL = AUTH_BASE + "/oauth2/token"
REGISTER_URL = AUTH_BASE + "/oauth2/register"
SCOPES = "openid email offline_access"
REDIRECT = "http://localhost:8765/callback"
STATE_TTL = 15 * 60
PROTOCOL = "2025-06-18"
POLL_LIMIT_SECONDS = 6 * 60
RETRY_STATUSES = (502, 503, 504)
SAFE_SUBMIT_RETRY = (503,)
RETRY_WAITS = (10, 20)
POLL_ERROR_WAIT = 8
DOWNLOAD_TRIES = 3
MAX_REFERENCES = 4
MAX_RESULT_BYTES = 40 * 1024 * 1024

_states: dict[str, dict] = {}  # one-use login states (single process)
_token_lock = threading.Lock()


class HiggsfieldError(RuntimeError):
    def __init__(self, message: str, charged: bool = False):
        super().__init__(message)
        self.charged = charged


# ---------------- connection ----------------

def status() -> dict:
    return {"connected": bool(secrets_store.get("hf_refresh_token")), "email": secrets_store.get("hf_email"),
            "settings": f"{HIGGSFIELD_MODEL} {HIGGSFIELD_VARIANT} · {HIGGSFIELD_QUALITY} · {HIGGSFIELD_RESOLUTION}"}


def start_login(user_id: int) -> str:
    r = requests.post(REGISTER_URL, json={
        "client_name": "Ads Planner", "redirect_uris": [REDIRECT],
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
        "token_endpoint_auth_method": "none", "scope": SCOPES}, timeout=20)
    if r.status_code >= 400:
        raise HiggsfieldError(f"Higgsfield refused to register the app ({r.status_code}).")
    client_id = r.json()["client_id"]
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(24)
    now = time.time()
    for k in [k for k, v in _states.items() if v["at"] < now - STATE_TTL]:
        _states.pop(k, None)
    _states[state] = {"verifier": verifier, "client_id": client_id, "user": user_id, "at": now}
    return AUTHORIZE_URL + "?" + urlencode({
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT, "scope": SCOPES, "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256", "resource": HIGGSFIELD_MCP_URL,
        "prompt": "consent"})


def complete_login(pasted_url: str, user_id: int) -> str:
    """Finishes the login from the localhost address the browser landed on. Returns the email."""
    q = parse_qs(urlparse((pasted_url or "").strip()).query)
    code, state, error = ((q.get(k) or [""])[0] for k in ("code", "state", "error"))
    if error:
        raise HiggsfieldError("Higgsfield access was not allowed. Click Connect again.")
    if not code or not state:
        raise HiggsfieldError("That is not the address from the Higgsfield login: copy the whole address "
                              "(it starts with http://localhost:8765/callback?code=).")
    started = _states.pop(state, None)
    if not started or started["at"] < time.time() - STATE_TTL:
        raise HiggsfieldError("This login has expired or was used. Click Connect again.")
    if started["user"] != user_id:
        raise HiggsfieldError("This login was started by someone else.")
    r = requests.post(TOKEN_URL, data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
        "client_id": started["client_id"], "code_verifier": started["verifier"], "resource": HIGGSFIELD_MCP_URL},
        timeout=20)
    if r.status_code >= 400:
        raise HiggsfieldError(f"Higgsfield login failed ({r.status_code}).")
    tok = r.json()
    if not tok.get("refresh_token"):
        raise HiggsfieldError("Higgsfield sent no refresh token.")
    email = _email_of(tok)
    secrets_store.put({"hf_client_id": started["client_id"], "hf_refresh_token": tok["refresh_token"],
                       "hf_access_token": tok.get("access_token", ""),
                       "hf_expires_at": str(time.time() + int(tok.get("expires_in", 3600))),
                       "hf_email": email or ""})
    return email or ""


def disconnect() -> None:
    secrets_store.delete("hf_client_id", "hf_refresh_token", "hf_access_token", "hf_expires_at", "hf_email")


def _email_of(tok: dict) -> str | None:
    try:
        p = tok["id_token"].split(".")[1]
        return json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4))).get("email")
    except (KeyError, IndexError, ValueError):
        return None


def access_token() -> str:
    def fresh():
        at, exp = secrets_store.get("hf_access_token"), float(secrets_store.get("hf_expires_at") or 0)
        return at if at and exp > time.time() + 90 else ""

    if not secrets_store.get("hf_refresh_token"):
        raise HiggsfieldError("Higgsfield is not connected: connect it on the Connections page.")
    if fresh():
        return fresh()
    # The refresh token rotates: two threads must never send the same one.
    with _token_lock:
        if fresh():
            return fresh()
        r = requests.post(TOKEN_URL, data={"grant_type": "refresh_token",
                                           "refresh_token": secrets_store.get("hf_refresh_token"),
                                           "client_id": secrets_store.get("hf_client_id"),
                                           "resource": HIGGSFIELD_MCP_URL}, timeout=20)
        if r.status_code >= 400:
            raise HiggsfieldError(f"Higgsfield login expired ({r.status_code}): connect it again on the Connections page.")
        tok = r.json()
        vals = {"hf_access_token": tok["access_token"],
                "hf_expires_at": str(time.time() + int(tok.get("expires_in", 3600)))}
        if tok.get("refresh_token"):
            vals["hf_refresh_token"] = tok["refresh_token"]
        secrets_store.put(vals)
        return tok["access_token"]


# ---------------- MCP ----------------

def _read_rpc(resp, rid: int) -> dict:
    if "text/event-stream" in resp.headers.get("content-type", ""):
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                try:
                    msg = json.loads(line[5:].strip())
                except ValueError:
                    continue
                if msg.get("id") == rid:
                    return msg
        raise HiggsfieldError("Higgsfield sent no answer.")
    return resp.json()


class McpSession:
    def __init__(self, http: requests.Session, token: str):
        self.http, self.token, self.session_id, self._id = http, token, None, 0

    def _headers(self) -> dict:
        h = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
             "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": PROTOCOL}
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    def _rpc(self, method: str, params: dict, retry_on: tuple = RETRY_STATUSES) -> dict:
        self._id += 1
        body = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
        for wait in (*RETRY_WAITS, None):
            r = self.http.post(HIGGSFIELD_MCP_URL, headers=self._headers(), json=body, timeout=60)
            if r.status_code not in retry_on or wait is None:
                break
            log.info("Higgsfield %s answered %s; trying again in %ss", method, r.status_code, wait)
            time.sleep(wait)
        if r.status_code == 401:
            raise HiggsfieldError("Higgsfield login expired: connect it again on the Connections page.")
        if r.status_code >= 400:
            raise HiggsfieldError(f"Higgsfield refused ({r.status_code}).")
        self.session_id = r.headers.get("mcp-session-id") or self.session_id
        msg = _read_rpc(r, self._id)
        if msg.get("error"):
            raise HiggsfieldError(f"Higgsfield: {str(msg['error'].get('message', 'error'))[:200]}")
        return msg.get("result") or {}

    def start(self) -> None:
        self._rpc("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                 "clientInfo": {"name": "ads-planner", "version": "1.0"}})
        self.http.post(HIGGSFIELD_MCP_URL, headers=self._headers(),
                       json={"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout=30)

    def tool(self, name: str, args: dict) -> Any:
        retry_on = SAFE_SUBMIT_RETRY if name == "generate_image" else RETRY_STATUSES
        res = self._rpc("tools/call", {"name": name, "arguments": args}, retry_on)
        texts = [c.get("text", "") for c in res.get("content", []) if c.get("type") == "text"]
        if res.get("isError"):
            raise HiggsfieldError(f"Higgsfield {name}: {' '.join(texts)[:240]}")
        if res.get("structuredContent") is not None:
            return res["structuredContent"]
        for t in texts:
            try:
                return json.loads(t)
            except ValueError:
                continue
        return " ".join(texts)


def find_values(data: Any, keys: tuple) -> list[str]:
    out: list[str] = []

    def walk(v: Any, key: str = "") -> None:
        if isinstance(v, dict):
            for k, child in v.items():
                walk(child, k)
        elif isinstance(v, list):
            for child in v:
                walk(child, key)
        elif isinstance(v, str) and key in keys and v:
            out.append(v)

    walk(data)
    return out


def result_urls(data: Any) -> list[str]:
    urls: list[str] = []

    def walk(v: Any) -> None:
        if isinstance(v, dict):
            for child in v.values():
                walk(child)
        elif isinstance(v, list):
            for child in v:
                walk(child)
        elif isinstance(v, str) and v.startswith("https://") and \
                any(x in v.lower().split("?")[0] for x in (".png", ".jpg", ".jpeg", ".webp")):
            urls.append(v)

    walk(data)
    return urls


def _download(http: requests.Session, url: str) -> bytes | None:
    with http.get(url, timeout=60, stream=True) as r:
        if r.status_code >= 400:
            return None
        body = b""
        for chunk in r.iter_content(65536):
            body += chunk
            if len(body) > MAX_RESULT_BYTES:
                return None
        return body or None


# ---------------- one picture ----------------

def make_picture(reference_urls: list[str], prompt: str, aspect_ratio: str | None = None) -> bytes:
    """References are public https URLs (product photo). Raises HiggsfieldError (safe to show)."""
    token = access_token()
    with requests.Session() as http:
        mcp = McpSession(http, token)
        mcp.start()
        medias = []
        for url in reference_urls[:MAX_REFERENCES]:
            ids = find_values(mcp.tool("media_import_url", {"url": url, "type": "image"}), ("media_id", "id"))
            if ids:
                medias.append({"role": "image_references", "value": ids[0]})
        if reference_urls and len(medias) < min(len(reference_urls), MAX_REFERENCES):
            raise HiggsfieldError("Higgsfield could not import every reference picture.")  # never half-blind
        params: dict[str, Any] = {"model": HIGGSFIELD_MODEL, "prompt": prompt,
                                  "aspect_ratio": aspect_ratio or HIGGSFIELD_ASPECT_RATIO,
                                  "variant": HIGGSFIELD_VARIANT, "quality": HIGGSFIELD_QUALITY,
                                  "resolution": HIGGSFIELD_RESOLUTION, "background": HIGGSFIELD_BACKGROUND,
                                  "count": 1, "use_unlim": False}
        if medias:
            params["medias"] = medias
        ctx = "Meta ad images for an online store"
        try:
            try:
                res = mcp.tool("generate_image", {"params": params, "context": ctx})
            except HiggsfieldError as exc:
                # a shape the model does not offer is refused before any job starts: fall back to square
                if "aspect" not in str(exc).lower() or params["aspect_ratio"] == "1:1":
                    raise
                log.warning("Higgsfield refused aspect %s, using 1:1", params["aspect_ratio"])
                params["aspect_ratio"] = "1:1"
                res = mcp.tool("generate_image", {"params": params, "context": ctx})
        except requests.ConnectionError as exc:
            raise HiggsfieldError("Could not reach Higgsfield.") from exc
        except requests.RequestException as exc:  # sent, answer lost: the job may exist (billed)
            raise HiggsfieldError(f"Lost contact with Higgsfield ({type(exc).__name__}).", charged=True) from exc
        except HiggsfieldError as exc:
            if any(f"({s})" in str(exc) for s in RETRY_STATUSES if s not in SAFE_SUBMIT_RETRY):
                raise HiggsfieldError(str(exc), charged=True) from exc
            raise
        job_ids = find_values(res, ("job_id", "jobId", "id"))
        if not job_ids:
            raise HiggsfieldError(f"Higgsfield started no job: {str(res)[:200]}")
        return _wait_and_download(mcp, http, job_ids[0])


def _wait_and_download(mcp: McpSession, http: requests.Session, job_id: str) -> bytes:
    deadline = time.monotonic() + POLL_LIMIT_SECONDS
    last_error = ""
    while time.monotonic() < deadline:
        try:
            res = mcp.tool("jobs_wait", {"jobs": [{"index": 0, "job_id": job_id}], "timeout_seconds": 15})
        except (requests.RequestException, HiggsfieldError) as exc:
            if isinstance(exc, HiggsfieldError) and "login expired" in str(exc):
                raise HiggsfieldError(str(exc), charged=True) from exc
            last_error = type(exc).__name__ if isinstance(exc, requests.RequestException) else str(exc)[:120]
            time.sleep(POLL_ERROR_WAIT)
            continue
        statuses = [s.lower() for s in find_values(res, ("status",))]
        if any(s in ("failed", "cancelled", "canceled", "nsfw") for s in statuses):
            raise HiggsfieldError(f"Higgsfield could not make the picture ({statuses[-1]}).")
        urls = result_urls(res)
        if urls and (not statuses or statuses[-1] == "completed"):
            for attempt in range(DOWNLOAD_TRIES):
                try:
                    got = _download(http, urls[0])
                except requests.RequestException:
                    got = None
                if got:
                    return got
                if attempt + 1 < DOWNLOAD_TRIES:
                    time.sleep(POLL_ERROR_WAIT)
            raise HiggsfieldError("The Higgsfield picture could not be downloaded.", charged=True)
    if last_error:
        raise HiggsfieldError(f"Lost contact with Higgsfield ({last_error}).", charged=True)
    raise HiggsfieldError("Higgsfield took too long (over 6 minutes).", charged=True)


def generate_with_reference(prompt: str, reference: tuple[bytes, str] | None, aspect_ratio: str) -> bytes:
    """Puts the product photo on R2 for the length of one job, makes the picture, removes it again."""
    from . import r2

    if reference is None:
        return make_picture([], prompt, aspect_ratio)
    if not r2.configured():
        raise HiggsfieldError("Higgsfield needs the product photo at a public URL: set the R2_* values in .env "
                              "(the Social Agent's bucket works).")
    key, url = r2.put_temp(*reference)
    try:
        return make_picture([url], prompt, aspect_ratio)
    finally:
        try:
            r2.delete(key)
        except Exception:  # a leftover reference must not fail the picture
            log.warning("Could not remove Higgsfield reference %s", key)
