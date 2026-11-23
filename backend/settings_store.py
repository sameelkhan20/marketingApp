"""Settings stored in the DB and edited on the Connections page. The Meta token is kept encrypted
in secrets_store, never in plain text."""

from sqlalchemy.orm import Session

from .db import Setting

META_KEYS = [
    "meta_access_token",   # long-lived user token or system-user token
    "meta_ad_account_id",  # act_123... or 123...
    "meta_page_id",        # Facebook Page used as the ad's identity
    "meta_pixel_id",       # Dataset/pixel id for Purchase optimisation
    "meta_ig_account_id",  # optional Instagram account id
    "store_url",           # landing page for ads
    "creative_image_urls", # comma-separated image URLs used for ads
    "auto_actions",        # JSON: {"no_purchases": true, ...} alert kinds the monitor may act on itself
    "auto_max_budget_pct", # guardrail: max budget change per action (default 20)
    "product_photo",       # file name in data/media used as the reference for AI creatives
    "brand_style",         # free text: colours, vibe, what to avoid (fed into image prompts)
    "last_digest_at",      # ISO date of the last weekly digest email
    "aud_pixel", "aud_visitors", "aud_buyers",  # retargeting audiences created on Meta, reused per pixel
]

SECRET_KEYS = {"meta_access_token"}

# Keys the Meta-tab form may write. Internal keys (product_photo, auto_actions, last_digest_at...)
# have their own endpoints with validation.
FORM_KEYS = {"meta_access_token", "meta_ad_account_id", "meta_page_id", "meta_pixel_id",
             "meta_ig_account_id", "store_url", "creative_image_urls"}


def _migrate_plain_secrets(session: Session) -> None:
    """Move a token saved in plain text by an older version into the encrypted store."""
    from . import secrets_store
    for k in SECRET_KEYS:
        row = session.get(Setting, k)
        if row and row.value:
            secrets_store.put({k: row.value})
            row.value = ""
            session.commit()


def get_all(session: Session) -> dict[str, str]:
    from . import secrets_store
    _migrate_plain_secrets(session)
    rows = {s.key: s.value for s in session.query(Setting).all()}
    out = {k: rows.get(k, "") for k in META_KEYS}
    for k in SECRET_KEYS:
        out[k] = secrets_store.get(k)
    return out


def get(session: Session, key: str) -> str:
    if key in SECRET_KEYS:
        from . import secrets_store
        _migrate_plain_secrets(session)
        return secrets_store.get(key)
    row = session.get(Setting, key)
    return row.value if row else ""


def set_many(session: Session, values: dict[str, str]) -> None:
    # secrets first: secrets_store commits in its own session, which must not wait on this one
    values = dict(sorted(values.items(), key=lambda kv: kv[0] not in SECRET_KEYS))
    for k, v in values.items():
        if k not in META_KEYS:
            continue
        if k in SECRET_KEYS and v == "********":
            continue  # masked value sent back unchanged
        if k in SECRET_KEYS:
            from . import secrets_store
            secrets_store.put({k: v.strip()})
            continue
        row = session.get(Setting, k)
        if row:
            row.value = v.strip()
        else:
            session.add(Setting(key=k, value=v.strip()))
    session.commit()


def masked(values: dict[str, str]) -> dict[str, str]:
    return {k: ("********" if k in SECRET_KEYS and v else v) for k, v in values.items()}


def ad_account_path(account_id: str) -> str:
    account_id = account_id.strip()
    return account_id if account_id.startswith("act_") else f"act_{account_id}"
