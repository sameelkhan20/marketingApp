"""Small key/value store for OAuth tokens (Higgsfield, Gmail), kept in the settings table but never
exposed by the settings API. Values are encrypted with a key derived from the app's JWT secret, so a
copied database alone does not reveal the tokens."""

import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken

from .db import Setting, get_session


def _fernet() -> Fernet:
    from .auth.security import ACCESS_TOKEN_SECRET
    key = hashlib.sha256(("token-store:" + ACCESS_TOKEN_SECRET).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def put(values: dict[str, str]) -> None:
    f = _fernet()
    with get_session() as s:
        for k, v in values.items():
            key = f"secret:{k}"
            row = s.get(Setting, key)
            enc = f.encrypt(v.encode()).decode() if v else ""
            if row:
                row.value = enc
            else:
                s.add(Setting(key=key, value=enc))
        s.commit()


def get(name: str) -> str:
    with get_session() as s:
        row = s.get(Setting, f"secret:{name}")
    if not row or not row.value:
        return ""
    try:
        return _fernet().decrypt(row.value.encode()).decode()
    except InvalidToken:
        logging.getLogger("secrets").error("stored secret %s cannot be decrypted (was ACCESS_TOKEN_SECRET changed?); "
                                           "connect it again", name)
        return ""


def delete(*names: str) -> None:
    with get_session() as s:
        for n in names:
            row = s.get(Setting, f"secret:{n}")
            if row:
                s.delete(row)
        s.commit()
