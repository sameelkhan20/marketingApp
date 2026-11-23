"""Ported from core_auth_fixed/app/security.py.

Dual-token key separation: access and refresh tokens signed with different
HS256 secrets. Secrets come from .env; if absent they are generated once and
stored in data/jwt_secrets.json so the app runs out of the box.
"""

import json
import os
import secrets
from datetime import datetime, timedelta, timezone

import jwt
from passlib.context import CryptContext

from ..config import DATA_DIR

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", 60))
REFRESH_TOKEN_EXPIRE_DAYS = int(os.getenv("REFRESH_TOKEN_EXPIRE_DAYS", 7))


def _load_secrets() -> tuple[str, str]:
    a, r = os.getenv("ACCESS_TOKEN_SECRET", ""), os.getenv("REFRESH_TOKEN_SECRET", "")
    if a and r:
        return a, r
    path = DATA_DIR / "jwt_secrets.json"
    if path.exists():
        d = json.loads(path.read_text())
        return d["access"], d["refresh"]
    d = {"access": secrets.token_hex(32), "refresh": secrets.token_hex(32)}
    path.write_text(json.dumps(d))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return d["access"], d["refresh"]


ACCESS_TOKEN_SECRET, REFRESH_TOKEN_SECRET = _load_secrets()

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    # jti makes every token unique, so a refreshed token never equals a blacklisted one
    to_encode.update({"exp": expire, "token_type": "access", "jti": secrets.token_hex(8)})
    return jwt.encode(to_encode, ACCESS_TOKEN_SECRET, algorithm=ALGORITHM)


def create_refresh_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    to_encode.update({"exp": expire, "token_type": "refresh", "jti": secrets.token_hex(8)})
    return jwt.encode(to_encode, REFRESH_TOKEN_SECRET, algorithm=ALGORITHM)
