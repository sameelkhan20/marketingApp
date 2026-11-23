"""Ported from core_auth_fixed/app/dependencies.py.

Zero-trust check order kept: blacklist -> signature -> token type -> user active.
Blacklist lives in SQLite (token_blacklist) instead of Redis.
"""

import hashlib
import time

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import delete, select

from ..db import TokenBlacklist, User, get_session
from .security import ACCESS_TOKEN_SECRET, ALGORITHM

security_scheme = HTTPBearer(auto_error=False)

CREDENTIALS_EXCEPTION = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


_last_cleanup = [0.0]


def is_blacklisted(token: str) -> bool:
    """Read-only on the hot path; expired rows are purged at most every 10 minutes."""
    with get_session() as s:
        if time.time() - _last_cleanup[0] > 600:
            _last_cleanup[0] = time.time()
            s.execute(delete(TokenBlacklist).where(TokenBlacklist.expires_at < int(time.time())))
            s.commit()
        return s.get(TokenBlacklist, _key(token)) is not None


def _key(token: str) -> str:
    """Rows hold a hash, not the token: short, fixed size and useless if read."""
    return hashlib.sha256(token.encode()).hexdigest()


def blacklist_token(token: str, expires_at: int) -> bool:
    """True when this call added the row (False: it was already revoked). Used to make refresh
    rotation single-use even when two requests race."""
    from sqlalchemy.exc import IntegrityError
    with get_session() as s:
        if s.get(TokenBlacklist, _key(token)):
            return False
        s.add(TokenBlacklist(token=_key(token), expires_at=expires_at))
        try:
            s.commit()
        except IntegrityError:
            return False
        return True


def user_from_token(token: str) -> dict:
    if is_blacklisted(token):
        raise CREDENTIALS_EXCEPTION
    try:
        payload = jwt.decode(token, ACCESS_TOKEN_SECRET, algorithms=[ALGORITHM])
        email = payload.get("sub")
        if email is None or payload.get("token_type") != "access":
            raise CREDENTIALS_EXCEPTION
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token has expired",
                            headers={"WWW-Authenticate": "Bearer"})
    except jwt.InvalidTokenError:
        raise CREDENTIALS_EXCEPTION

    with get_session() as s:
        user = s.scalar(select(User).where(User.email == email))
        if user is None:
            raise CREDENTIALS_EXCEPTION
        if not user.is_active:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="This account is locked",
                                headers={"WWW-Authenticate": "Bearer"})
        if int(payload.get("tv", 0)) != int(user.token_version or 0):
            raise CREDENTIALS_EXCEPTION  # issued before the last password change
        return {"id": user.id, "email": user.email, "is_active": bool(user.is_active),
                "is_superuser": bool(user.is_superuser), "created_at": user.created_at}


async def get_current_user(creds: HTTPAuthorizationCredentials | None = Depends(security_scheme)) -> dict:
    if creds is None:
        raise CREDENTIALS_EXCEPTION
    return user_from_token(creds.credentials)


async def get_current_superuser(user: dict = Depends(get_current_user)) -> dict:
    if not user["is_superuser"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Superuser required")
    return user
