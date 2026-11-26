"""The store's products: read from the store's own site (Shopify or WooCommerce), joined with what each
product did in Meta ads, and ranked so the owner can see which product to advertise next.

Site: Shopify's public /products.json, or WooCommerce's public Store API. No keys are needed, but a bot
challenge (Cloudflare "Just a moment...") blocks both; the owner has to allow the app through it.
Meta: account insights broken down by product (catalogue ads). Each row's product_id is
"<retailer id>, <product name>"; on Shopify the retailer id is the variant id.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from urllib.parse import urlsplit

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import StoreProduct

UA = {"User-Agent": "Mozilla/5.0 (compatible; AdsPlanner product reader)", "Accept": "application/json"}
MAX_PAGES = 20          # 20 x 250 Shopify products, 20 x 100 WooCommerce products
TIMEOUT = 20


class ProductError(Exception):
    pass


# ---------- reading the site ----------

def _base(store_url: str) -> str:
    u = urlsplit((store_url or "").strip())
    if u.scheme not in ("http", "https") or not u.netloc:
        raise ProductError("Set the store URL (like https://yourstore.com) first.")
    return f"{u.scheme}://{u.netloc}"


def _get_json(url: str):
    try:
        r = requests.get(url, headers=UA, timeout=TIMEOUT)
    except requests.RequestException as ex:
        raise ProductError(f"Could not reach the store ({type(ex).__name__}).")
    ctype = r.headers.get("content-type", "")
    if "json" not in ctype:
        if r.status_code in (403, 429, 503) or "Just a moment" in r.text[:2000]:
            raise ProductError("The store's bot protection (Cloudflare or similar) blocked the app. Allow the app "
                               "through it, or turn the challenge off for the product list, then try again.")
        return None  # not this platform
    try:
        return r.json()
    except ValueError:
        return None


def _shopify(base: str) -> list[dict] | None:
    out: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        data = _get_json(f"{base}/products.json?limit=250&page={page}")
        if not isinstance(data, dict) or "products" not in data:
            return None if page == 1 else out
        items = data["products"]
        if not items:
            break
        for p in items:
            variants = p.get("variants") or []
            prices = [float(v["price"]) for v in variants if v.get("price") not in (None, "")]
            ids = [str(p.get("id", ""))] + [str(v.get("id", "")) for v in variants] + \
                  [str(v.get("sku")) for v in variants if v.get("sku")]
            out.append({
                "key": f"shopify:{p.get('id')}",
                "title": (p.get("title") or "")[:200],
                "url": f"{base}/products/{p.get('handle', '')}",
                "image": ((p.get("images") or [{}])[0].get("src") or "")[:500],
                "site_price": min(prices) if prices else 0.0,
                "currency": "",  # Shopify's public list has no currency; the owner enters the USD price
                "in_stock": any(v.get("available", True) for v in variants) if variants else True,
                "ids": [i for i in ids if i],
                "kind": (p.get("product_type") or "")[:80],
            })
        if len(items) < 250:
            break
    return out


def _woo(base: str) -> list[dict] | None:
    out: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        data = _get_json(f"{base}/wp-json/wc/store/v1/products?per_page=100&page={page}")
        if not isinstance(data, list):
            return None if page == 1 else out
        if not data:
            break
        for p in data:
            pr = p.get("prices") or {}
            minor = int(pr.get("currency_minor_unit", 2) or 0)
            try:
                price = int(pr.get("price") or 0) / (10 ** minor)
            except (TypeError, ValueError):
                price = 0.0
            pid = str(p.get("id", ""))
            ids = [pid, f"wc_post_id_{pid}"] + ([str(p["sku"]), f"{p['sku']}_{pid}"] if p.get("sku") else [])
            out.append({
                "key": f"woo:{pid}",
                "title": re.sub(r"<[^>]+>", "", p.get("name") or "")[:200],
                "url": (p.get("permalink") or "")[:500],
                "image": ((p.get("images") or [{}])[0].get("src") or "")[:500],
                "site_price": price,
                "currency": (pr.get("currency_code") or "")[:3],
                "in_stock": bool(p.get("is_in_stock", True)),
                "ids": [i for i in ids if i],
                "kind": ", ".join(c.get("name", "") for c in (p.get("categories") or []))[:80],
            })
        if len(data) < 100:
            break
    return out


def read_site(store_url: str) -> tuple[str, list[dict]]:
    """(platform, products) from the store's own site."""
    base = _base(store_url)
    for platform, reader in (("shopify", _shopify), ("woocommerce", _woo)):
        items = reader(base)
        if items is not None:
            return platform, items
    raise ProductError("This site does not look like a Shopify or WooCommerce store (no public product list).")


# ---------- what each product did in Meta ads ----------

def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def meta_by_product(rows: list[dict], currency: str) -> dict[str, dict]:
    """{retailer id: {name, spend_usd, impressions, clicks, purchases, revenue_usd}} from insights rows."""
    from .meta_sync import _purchases, to_usd
    out: dict[str, dict] = {}
    for r in rows:
        raw = str(r.get("product_id") or "")
        rid, _, name = raw.partition(", ")
        rid = rid.strip()
        if not rid:
            continue
        a = out.setdefault(rid, {"name": "", "spend_usd": 0.0, "impressions": 0, "clicks": 0,
                                 "purchases": 0, "revenue_usd": 0.0})
        if name and not a["name"]:
            a["name"] = name.strip()[:200]
        pur, rev = _purchases(r)
        a["spend_usd"] += to_usd(float(r.get("spend", 0) or 0), currency)
        a["impressions"] += int(r.get("impressions", 0) or 0)
        a["clicks"] += int(r.get("inline_link_clicks", 0) or 0)
        a["purchases"] += pur
        a["revenue_usd"] += to_usd(rev, currency)
    return out


def match(products: list[dict], by_id: dict[str, dict]) -> tuple[dict[str, dict], int]:
    """{product key: summed Meta results}, and how many Meta products matched nothing on the site."""
    id_owner = {i: p["key"] for p in products for i in p["ids"]}
    title_owner = {_norm(p["title"]): p["key"] for p in products if p["title"]}
    out: dict[str, dict] = {}
    unmatched = 0
    for rid, m in by_id.items():
        key = id_owner.get(rid) or title_owner.get(_norm(m["name"]))
        if not key:
            unmatched += 1
            continue
        t = out.setdefault(key, {"spend_usd": 0.0, "impressions": 0, "clicks": 0, "purchases": 0, "revenue_usd": 0.0})
        for k in t:
            t[k] += m[k]
    for t in out.values():
        t["spend_usd"] = round(t["spend_usd"], 2)
        t["revenue_usd"] = round(t["revenue_usd"], 2)
    return out, unmatched


# ---------- sync: site + Meta into the table ----------

def sync(session: Session, brand_id: int, years: int = 2) -> dict:
    from . import brands
    from .meta_client import MetaClient, MetaError
    from .settings_store import ad_account_path
    vals = brands.settings_for(session, brand_id)
    platform, items = read_site(vals.get("store_url", ""))

    meta: dict[str, dict] = {}
    unmatched, meta_note = 0, ""
    if vals.get("meta_access_token") and vals.get("meta_ad_account_id"):
        try:
            client = MetaClient(vals["meta_access_token"])
            act = ad_account_path(vals["meta_ad_account_id"])
            currency = client.account(act).get("currency", "USD")
            until = date.today()
            rows = client.product_insights(act, (until - timedelta(days=365 * years)).isoformat(), until.isoformat())
            meta, unmatched = match(items, meta_by_product(rows, currency))
        except MetaError as ex:
            meta_note = f"Meta results could not be read: {ex}"
    else:
        meta_note = "Connect this store to Meta to see which products sell in ads."

    keep = {p.key: p for p in session.scalars(select(StoreProduct).where(StoreProduct.brand_id == brand_id))}
    now = datetime.utcnow()
    seen = set()
    for it in items:
        row = keep.get(it["key"]) or StoreProduct(brand_id=brand_id, key=it["key"])
        row.title, row.url, row.image = it["title"], it["url"], it["image"]
        row.site_price, row.currency, row.in_stock = it["site_price"], it["currency"], 1 if it["in_stock"] else 0
        row.ids, row.kind = it["ids"], it["kind"]
        row.meta = meta.get(it["key"], {})
        row.synced_at = now
        if row.price_usd in (None, 0) and it["currency"] == "USD" and it["site_price"]:
            row.price_usd = it["site_price"]  # a USD store's price is already the plan's price
        session.add(row)
        seen.add(it["key"])
    for k, row in keep.items():
        if k not in seen:
            session.delete(row)  # gone from the site
    session.commit()
    return {"platform": platform, "products": len(items), "with_meta_results": len(meta),
            "meta_products_not_on_site": unmatched, "note": meta_note}


# ---------- ranking ----------

def assess(p: StoreProduct) -> tuple[float, str, str]:
    """(score, verdict, reason in plain words). Higher score = better to advertise next."""
    m = p.meta or {}
    spend, clicks, sales = float(m.get("spend_usd", 0)), int(m.get("clicks", 0)), int(m.get("purchases", 0))
    price, cost, ship = float(p.price_usd or 0), float(p.cost_usd or 0), float(p.shipping_usd or 0)
    margin = price - cost - ship if price and (cost or ship) else None
    cpa = spend / sales if sales else None

    if not p.in_stock:
        return -1000, "Out of stock", "Not in stock on the site: advertising it wastes clicks."
    if margin is not None and margin <= 0:
        return -500, "Loses money", f"Price ${price:.2f} does not cover cost and shipping (${cost + ship:.2f})."
    if sales >= 2 and (margin is None or cpa < margin):
        extra = f", keeping about ${margin - cpa:.2f} per order after ads" if margin is not None else ""
        return 1000 + sales * 10 + (margin - cpa if margin else 0), "Proven seller", \
            f"{sales} sales from ${spend:.0f} of ads (${cpa:.2f} per sale){extra}. Scale it."
    if sales >= 1 and margin is not None and cpa >= margin:
        return 80, "Sells, but ads cost too much", \
            f"{sales} sale(s) at ${cpa:.2f} each, more than the ${margin:.2f} you keep. Raise the price or try new ads."
    if sales == 1:
        return 400 + (margin or 0), "One sale so far", \
            f"1 sale from ${spend:.0f} of ads. Promising: worth a proper test with its own budget."
    if clicks >= 20:
        return 60, "Clicks, no sales", \
            f"{clicks} clicks from ${spend:.0f} of ads but no sale. Check the product page, price and photos first."
    if spend >= 20:
        return 20, "Spent without results", f"${spend:.0f} of ads with almost no clicks: the ad or the product did not catch on."
    score = 300 + (margin if margin is not None else 0) + (10 if p.image else -50)
    why = f"Never really advertised. You keep ${margin:.2f} per order" if margin is not None \
        else "Never really advertised. Enter its price and costs to see the margin"
    return score, "Not tested yet", why + "."


def ranked(session: Session, brand_id: int) -> list[dict]:
    rows = session.scalars(select(StoreProduct).where(StoreProduct.brand_id == brand_id)).all()
    out = []
    for p in rows:
        score, verdict, reason = assess(p)
        out.append({"key": p.key, "title": p.title, "url": p.url, "image": p.image, "site_price": p.site_price,
                    "currency": p.currency, "in_stock": bool(p.in_stock), "kind": p.kind,
                    "price_usd": p.price_usd or 0, "cost_usd": p.cost_usd or 0, "shipping_usd": p.shipping_usd or 0,
                    "meta": p.meta or {}, "score": round(score, 2), "verdict": verdict, "reason": reason,
                    "synced_at": p.synced_at.isoformat() if p.synced_at else ""})
    out.sort(key=lambda x: -x["score"])
    return out
