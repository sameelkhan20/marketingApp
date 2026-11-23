"""Ported from core_auth_fixed/app/routers/{auth,users}.py, mounted under /api.

Differences from the original:
- SQLAlchemy/SQLite instead of asyncpg; SQLite table instead of Redis blacklist.
- No public signup: /users/register always needs the owner's (superuser) token. The first owner
  is created on the server with `python -m scripts.create_owner` (create_user() below).
"""

import time

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from ..db import User, get_session
from .dependencies import blacklist_token, get_current_superuser, get_current_user, is_blacklisted, security_scheme, user_from_token
from .schemas import (LogoutRequest, StandardActionResponse, TokenExchangeResponse, TokenRefreshRequest,
                      UserCreate, UserLogin, UserRegistrationResponse)
from .security import (ACCESS_TOKEN_SECRET, ALGORITHM, REFRESH_TOKEN_SECRET, create_access_token, create_refresh_token,
                       get_password_hash, verify_password)

_DUMMY_HASH = get_password_hash("timing-equaliser-not-a-real-password")
_register_lock = __import__("threading").Lock()

auth_router = APIRouter(prefix="/api/auth", tags=["Authentication"])
users_router = APIRouter(prefix="/api/users", tags=["Users"])


def _user_count() -> int:
    with get_session() as s:
        return s.scalar(select(func.count()).select_from(User)) or 0


@auth_router.get("/setup-status")
def setup_status():
    # The login page only says whether an account exists; it never offers signup.
    return {"has_users": _user_count() > 0}


# Failed sign-ins per email and per IP (single worker, so memory is enough).
WINDOW, MAX_PER_PAIR, MAX_PER_IP, MAX_PER_EMAIL = 15 * 60, 5, 20, 100
_fails: dict[str, list[float]] = {}
_fail_lock = __import__("threading").Lock()


def _recent(key: str, now: float) -> list[float]:
    hits = [t for t in _fails.get(key, []) if now - t < WINDOW]
    if hits:
        _fails[key] = hits
    else:
        _fails.pop(key, None)
    return hits


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def _tokens(email: str, version: int) -> TokenExchangeResponse:
    claims = {"sub": email, "tv": version}
    return TokenExchangeResponse(access_token=create_access_token(claims),
                                 refresh_token=create_refresh_token(claims), token_type="bearer")


@auth_router.post("/login", response_model=TokenExchangeResponse)
def login(user_data: UserLogin, request: Request):
    now, ip = time.time(), _client_ip(request)
    email = user_data.email.lower()
    pair_key, email_key, ip_key = f"p:{email}|{ip}", f"e:{email}", f"i:{ip}"
    with _fail_lock:
        blocked = (len(_recent(pair_key, now)) >= MAX_PER_PAIR or len(_recent(ip_key, now)) >= MAX_PER_IP
                   or len(_recent(email_key, now)) >= MAX_PER_EMAIL)
        if not blocked:  # count the attempt before the slow password check, so a burst cannot slip through
            for k in (pair_key, email_key, ip_key):
                _fails.setdefault(k, []).append(now)
    if blocked:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                            detail="Too many wrong passwords. Wait 15 minutes and try again.")
    with get_session() as s:
        user = s.scalar(select(User).where(User.email == user_data.email))
        # Always run bcrypt so response time does not reveal whether the email exists.
        ok = verify_password(user_data.password, user.hashed_password if user else _DUMMY_HASH)
        if not user or not ok:
            with _fail_lock:
                first = len(_recent(pair_key, now)) == 1
            if first:  # one activity row per burst, not one per guess
                from .. import activity
                activity.record("auth", "login_failed", "Failed sign-in", f"email {user_data.email}, from {ip}",
                                "warning", user_data.email)
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect email or password")
        if not user.is_active:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Inactive user")
        email, version = user.email, int(user.token_version or 0)
    with _fail_lock:  # a correct password clears this person's count (not other IPs' attempts)
        _fails.pop(pair_key, None)
        for k in (email_key, ip_key):
            if _fails.get(k):
                _fails[k].pop()
    from .. import activity
    activity.record("auth", "login", "Signed in", "", "ok", email)
    return _tokens(email, version)


def _revoke(token: str, secret: str | None) -> None:
    """Blacklist a token until its expiry. Signature is checked when a secret is given."""
    try:
        payload = (jwt.decode(token, secret, algorithms=[ALGORITHM]) if secret
                   else jwt.decode(token, options={"verify_signature": False}))
        exp = int(payload.get("exp", 0))
        if exp > int(time.time()):
            blacklist_token(token, exp)
    except Exception:
        pass


@auth_router.post("/logout", response_model=StandardActionResponse)
def logout(body: LogoutRequest | None = None,
           creds: HTTPAuthorizationCredentials | None = Depends(security_scheme)):
    """Revokes the access token and, when sent, the refresh token, so neither works after sign-out."""
    if creds:
        _revoke(creds.credentials, ACCESS_TOKEN_SECRET)  # only real tokens get stored
    if body and body.refresh_token:
        _revoke(body.refresh_token, REFRESH_TOKEN_SECRET)
    return StandardActionResponse(detail="Revocation complete")


@auth_router.post("/refresh", response_model=TokenExchangeResponse)
def refresh_session(request_data: TokenRefreshRequest):
    if is_blacklisted(request_data.refresh_token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token revoked")
    try:
        payload = jwt.decode(request_data.refresh_token, REFRESH_TOKEN_SECRET, algorithms=[ALGORITHM])
        email = payload.get("sub")
        if email is None or payload.get("token_type") != "refresh":
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token usage")
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token")

    with get_session() as s:
        user = s.scalar(select(User).where(User.email == email, User.is_active == True))  # noqa: E712
        if not user:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User account restricted")
        version = int(user.token_version or 0)
    if int(payload.get("tv", 0)) != version:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Signed out after a password change")
    # Rotation: the refresh token just used can never be used again; of two racing requests only one wins.
    if not blacklist_token(request_data.refresh_token, int(payload["exp"])):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token revoked")
    return _tokens(email, version)


@users_router.post("/register", response_model=UserRegistrationResponse, status_code=status.HTTP_201_CREATED)
def register_user(user: UserCreate, creds: HTTPAuthorizationCredentials | None = Depends(security_scheme)):
    """Only a signed-in owner can add users. There is no public signup."""
    if creds is None or not user_from_token(creds.credentials)["is_superuser"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the owner can add users")
    try:
        row = create_user(user.email, user.password, superuser=False)
    except ValueError as ex:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(ex))
    from .. import activity
    activity.record("auth", "user_added", f"Team member added: {row['email']}", "", "ok")
    return UserRegistrationResponse(**row)


def create_user(email: str, password: str, superuser: bool) -> dict:
    """Used by the owner's API call and by the server command that creates the first owner."""
    hashed = get_password_hash(password)
    with _register_lock, get_session() as s:
        if s.scalar(select(User).where(User.email == email)):
            raise ValueError("Email already registered")
        row = User(email=email, hashed_password=hashed, is_superuser=superuser)
        s.add(row)
        s.commit()
        s.refresh(row)
        return {"id": row.id, "email": row.email, "is_active": bool(row.is_active),
                "is_superuser": bool(row.is_superuser), "created_at": row.created_at}


@users_router.get("/me", response_model=UserRegistrationResponse)
def get_me(current_user: dict = Depends(get_current_user)):
    return UserRegistrationResponse(**current_user)


# ---------- account and team (owner adds people; there is still no public signup) ----------

class PasswordChange(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=200)


@users_router.post("/me/password", response_model=TokenExchangeResponse)
def change_password(body: PasswordChange, current_user: dict = Depends(get_current_user)):
    with get_session() as s:
        user = s.get(User, current_user["id"])
        if not verify_password(body.current_password, user.hashed_password):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="The current password is wrong")
        if body.current_password == body.new_password:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="The new password is the same as the old one")
        user.hashed_password = get_password_hash(body.new_password)
        user.token_version = int(user.token_version or 0) + 1  # signs out every other device
        version = user.token_version
        s.commit()
    from .. import activity
    activity.record("auth", "password_changed", "Password changed", "Other devices were signed out", "ok", current_user["email"])
    return _tokens(current_user["email"], version)


@users_router.get("", response_model=list[UserRegistrationResponse])
def list_users(_owner: dict = Depends(get_current_superuser)):
    with get_session() as s:
        return [UserRegistrationResponse(id=u.id, email=u.email, is_active=bool(u.is_active),
                                         is_superuser=bool(u.is_superuser), created_at=u.created_at)
                for u in s.scalars(select(User).order_by(User.id))]


class ActiveToggle(BaseModel):
    active: bool


@users_router.post("/{user_id}/active", response_model=StandardActionResponse)
def set_active(user_id: int, body: ActiveToggle, owner: dict = Depends(get_current_superuser)):
    if user_id == owner["id"]:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You cannot switch off your own account")
    with get_session() as s:
        user = s.get(User, user_id)
        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        user.is_active = body.active
        email = user.email
        s.commit()
    from .. import activity
    activity.record("auth", "user_access", f"{email} {'allowed in' if body.active else 'locked out'}", "", "ok", owner["email"])
    return StandardActionResponse(detail="Updated")
