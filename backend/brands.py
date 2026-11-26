"""Brands: one Meta connection per store (e.g. Trendy Jacket, Cutlery Nest), each with its own
token, ad account, page, pixel and store URL.

Brand 0 is the main store: the settings on the Connections page, exactly as before. Every other
brand is stored in the settings table (key "brands") and its token, encrypted, in secrets_store.
Each plan belongs to one brand (plans.brand_id); its launch, monitoring, readiness checks and
changes on Meta all use that brand's connection.
"""

import json
import threading

from sqlalchemy.orm import Session

from . import secrets_store, settings_store
from .db import Setting

BRAND_FIELDS = ["meta_ad_account_id", "meta_page_id", "meta_pixel_id", "meta_ig_account_id", "store_url",
                "creative_image_urls", "aud_pixel", "aud_visitors", "aud_buyers", "product_photo", "brand_style",
                "winners", "aud_account", "shop_domain"]
SECRET_FIELDS = ["meta_access_token", "shop_token", "woo_key", "woo_secret", "site_api_key"]  # encrypted, per store
FORM_FIELDS = {"name", "meta_access_token", "meta_ad_account_id", "meta_page_id", "meta_pixel_id",
               "meta_ig_account_id", "store_url", "creative_image_urls",
               "shop_domain", "shop_token", "woo_key", "woo_secret", "site_api_key"}


class BrandError(ValueError):
    pass


_lock = threading.RLock()  # the brands list is one JSON value: one writer at a time


def _load(session: Session) -> list[dict]:
    row = session.get(Setting, "brands")
    try:
        data = json.loads(row.value) if row and row.value else []
    except ValueError:
        data = []
    return [b for b in data if isinstance(b, dict) and isinstance(b.get("id"), int) and b["id"] > 0]


def _store(session: Session, brands: list[dict]) -> None:
    row = session.get(Setting, "brands")
    if row:
        row.value = json.dumps(brands)
    else:
        session.add(Setting(key="brands", value=json.dumps(brands)))
    session.commit()


def _token_key(brand_id: int, field: str = "meta_access_token") -> str:
    return f"brand{brand_id}:{field}"


def main_name(session: Session) -> str:
    row = session.get(Setting, "brand_main_name")
    return (row.value if row and row.value else "") or "Main store"


def settings_for(session: Session, brand_id: int | None) -> dict:
    """The same keys as settings_store.get_all, with this brand's connection in place of the main one."""
    base = settings_store.get_all(session)
    if not brand_id:
        return base
    b = next((x for x in _load(session) if x["id"] == brand_id), None)
    if b is None:
        raise BrandError(f"Brand #{brand_id} no longer exists; pick another brand for this plan.")
    out = dict(base)
    for k in BRAND_FIELDS:
        out[k] = b.get(k, "") or ""
    for k in SECRET_FIELDS:
        out[k] = secrets_store.get(_token_key(brand_id, k))
    return out


def all_brands(session: Session) -> list[dict]:
    """[{id, name, connected, account}], main store first."""
    main = settings_store.get_all(session)
    out = [{"id": 0, "name": main_name(session), "main": True,
            "connected": bool(main["meta_access_token"] and main["meta_ad_account_id"]),
            "account": main["meta_ad_account_id"]}]
    for b in _load(session):
        out.append({"id": b["id"], "name": b.get("name", f"Brand {b['id']}"), "main": False,
                    "connected": bool(secrets_store.get(_token_key(b["id"])) and b.get("meta_ad_account_id")),
                    "account": b.get("meta_ad_account_id", "")})
    return out


def brand_out(session: Session, brand_id: int) -> dict:
    """One brand for the edit form: every key masked, never sent back."""
    v = settings_for(session, brand_id)
    out = {"id": brand_id, "name": main_name(session) if not brand_id else
           next(b.get("name", "") for b in _load(session) if b["id"] == brand_id)}
    for k in FORM_FIELDS - {"name"}:
        out[k] = ("********" if v.get(k) else "") if k in SECRET_FIELDS else v.get(k, "")
    return out


def save(session: Session, brand_id: int | None, values: dict) -> int:
    """Create (brand_id None) or update a brand. Returns its id. Brand 0 is the main store."""
    values = {k: str(v).strip() for k, v in (values or {}).items() if k in FORM_FIELDS}
    name = values.pop("name", None)
    secrets = {k: values.pop(k) for k in SECRET_FIELDS if k in values}
    if brand_id == 0:
        if name:
            row = session.get(Setting, "brand_main_name")
            if row:
                row.value = name[:80]
            else:
                session.add(Setting(key="brand_main_name", value=name[:80]))
            session.commit()
        values.update(secrets)  # set_many keeps the masked ones and encrypts the rest
        settings_store.set_many(session, values)
        return 0
    with _lock:
        brands = _load(session)
        if brand_id is None:
            if not name:
                raise BrandError("Give the brand a name")
            # ids are never reused: a plan or launch of a deleted store can never land on a new one
            nxt = session.get(Setting, "brands_next_id")
            brand_id = max([b["id"] for b in brands] + [int(nxt.value) - 1 if nxt and nxt.value.isdigit() else 0]) + 1
            if nxt:
                nxt.value = str(brand_id + 1)
            else:
                session.add(Setting(key="brands_next_id", value=str(brand_id + 1)))
            b = {"id": brand_id}
            brands.append(b)
        else:
            b = next((x for x in brands if x["id"] == brand_id), None)
            if b is None:
                raise BrandError("Brand not found")
        if name:
            b["name"] = name[:80]
        for k, v in values.items():
            b[k] = v
        for k, v in secrets.items():
            if v != "********":
                secrets_store.put({_token_key(brand_id, k): v})  # an empty value clears it
        _store(session, brands)
        return brand_id


def set_fields(session: Session, brand_id: int, vals: dict) -> None:
    """Internal per-store values (retargeting audiences, product photo, brand style)."""
    vals = {k: v for k, v in vals.items() if k in BRAND_FIELDS}
    if not brand_id:
        settings_store.set_many(session, vals)
        return
    with _lock:
        brands = _load(session)
        b = next((x for x in brands if x["id"] == brand_id), None)
        if b is None:
            raise BrandError("Brand not found")
        b.update(vals)
        _store(session, brands)


def set_audiences(session: Session, brand_id: int, aud: dict) -> None:
    """Remember the retargeting audiences made on this brand's ad account."""
    try:
        set_fields(session, brand_id, {f"aud_{k}": aud.get(k, "") for k in ("pixel", "account", "visitors", "buyers")})
    except BrandError:
        pass


def delete(session: Session, brand_id: int) -> None:
    from .db import Launch, Plan
    if not brand_id:
        raise BrandError("The main store cannot be removed")
    plans = session.query(Plan).filter(Plan.brand_id == brand_id).all()
    waiting = [p.id for p in plans if p.status in ("draft", "approved", "launching")]
    if waiting:
        raise BrandError(f"Plans #{', #'.join(map(str, waiting))} use this store and are not launched: delete them first")
    running = [l.id for p in plans for l in session.query(Launch).filter(Launch.plan_id == p.id)
               if l.status not in ("completed", "archived", "deleted")]
    if running:
        raise BrandError("Campaigns of this store are still being tracked: close them on the Campaigns page first")
    _check_nothing_spends(session, brand_id, [l for p in plans for l in session.query(Launch).filter(Launch.plan_id == p.id)])
    with _lock:
        brands = [b for b in _load(session) if b["id"] != brand_id]
        secrets_store.delete(*(_token_key(brand_id, k) for k in SECRET_FIELDS))
        _store(session, brands)


def _check_nothing_spends(session: Session, brand_id: int, launches: list) -> None:
    """Without the store's token the app can no longer pause its campaigns, so make sure none runs."""
    if not launches:
        return
    from .meta_client import MetaClient, MetaError
    token = secrets_store.get(_token_key(brand_id))
    running = []
    try:
        client = MetaClient(token)
        for l in launches:
            if l.status == "deleted":
                continue
            st = (client.campaign_status(l.campaign_id) or {}).get("effective_status", "")
            if st in ("ACTIVE", "IN_PROCESS", "WITH_ISSUES", "PENDING_REVIEW", "PREAPPROVED", "PENDING_BILLING_INFO"):
                running.append(l.campaign_id)
    except MetaError as ex:
        raise BrandError(f"Could not check this store's campaigns on Meta ({ex}). Fix the connection, or pause its "
                         "campaigns in Ads Manager, before deleting the store.") from None
    if running:
        raise BrandError(f"Campaign(s) {', '.join(running)} still run on Meta. Pause them in Ads Manager first: "
                         "after the store is deleted the app can no longer stop them.")


def exists(session: Session, brand_id: int) -> bool:
    return brand_id == 0 or any(b["id"] == brand_id for b in _load(session))


def accounts(session: Session) -> set[str]:
    """Every connected ad account (normalised act_...)."""
    out = set()
    for b in all_brands(session):
        if b["account"]:
            out.add(settings_store.ad_account_path(b["account"]))
    return out


def with_landing(vals: dict, plan) -> dict:
    """The store's settings with the plan's own ad link (a product page) in place of the store URL."""
    link = ((getattr(plan, "brief", None) or {}).get("landing_url") or "").strip()
    return {**vals, "store_url": link} if link else vals


def launch_brand(session: Session, launch) -> int:
    """The brand a launch was made with (stored on the launch; older launches: their plan's)."""
    bid = (launch.objects or {}).get("brand_id")
    if bid is None:
        from .db import Plan
        plan = session.get(Plan, launch.plan_id)
        bid = plan.brand_id if plan else 0
    return int(bid or 0)
