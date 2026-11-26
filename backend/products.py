"""The store's products: read through the store's own API (Shopify or WooCommerce keys, backend/store_api.py)
with their stock, categories and real sales, joined with what each product did in Meta ads, and ranked so the
owner can see which product to advertise next.

Meta: account insights broken down by product (catalogue ads). Each row's product_id is
"<retailer id>, <product name>"; on Shopify the retailer id is the variant id.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import StoreProduct

TOP_BEST = 5           # the store's 5 best sellers (by real orders) are called best sellers


class ProductError(Exception):
    pass


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


_COMMON = {"the", "a", "an", "and", "with", "for", "of", "in", "by", "my", "free"}


def _closest(name: str, words: dict[str, set]) -> str | None:
    """The site product whose name shares most words with the Meta name, when it clearly stands out
    ("Full-Grain Leather BBQ Work Apron" vs "Full Grain Leather BBQ & Work Apron")."""
    want = set(_norm(name).split()) - _COMMON
    if not want:
        return None
    scored = sorted(((len(want & w) / len(want | w), k) for k, w in words.items() if w), reverse=True)
    if not scored or scored[0][0] < 0.6:
        return None
    if len(scored) > 1 and scored[0][0] - scored[1][0] < 0.1:
        return None  # two products fit about as well: do not guess
    return scored[0][1]


def match(products: list[dict], by_id: dict[str, dict]) -> tuple[dict[str, dict], int]:
    """{product key: summed Meta results}, and how many Meta products matched nothing on the site."""
    id_owner = {i: p["key"] for p in products for i in p["ids"]}
    title_owner = {_norm(p["title"]): p["key"] for p in products if p["title"]}
    words = {p["key"]: set(_norm(p["title"]).split()) - _COMMON for p in products if p["title"]}
    out: dict[str, dict] = {}
    unmatched = 0
    for rid, m in by_id.items():
        key = id_owner.get(rid) or title_owner.get(_norm(m["name"])) or _closest(m["name"], words)
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
    from .store_api import StoreAPIError, read_store
    vals = brands.settings_for(session, brand_id)
    try:
        store = read_store(vals)
    except StoreAPIError as ex:
        raise ProductError(str(ex))
    items = store["items"]

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

    # each product's home category: the smallest collection it is in, on a tie the one named like the product
    for it in items:
        words = set(_norm(it["title"]).split())
        mine = [c for c in store["cats"] if it["pid"] in c["pids"]]
        mine.sort(key=lambda c: (-len((set(_norm(c["name"]).split()) - {"apron", "aprons"}) & words),
                                 len(c["pids"]), len(c["name"])))
        if mine:
            it["kind"] = mine[0]["name"]
    save_site_categories(session, brand_id, [{"name": c["name"], "count": len(c["pids"])} for c in store["cats"]]
                         or _categories_from_kinds(items))

    # real sales: units sold per product, and its place among the store's best sellers
    sold = {it["key"]: store["sales"].get(it["pid"], {}) for it in items}
    order = sorted((k for k, v in sold.items() if v.get("units")), key=lambda k: -sold[k]["units"])
    rank = {k: i + 1 for i, k in enumerate(order)}

    keep = {p.key: p for p in session.scalars(select(StoreProduct).where(StoreProduct.brand_id == brand_id))}
    now = datetime.utcnow()
    seen = set()
    for it in items:
        row = keep.get(it["key"]) or StoreProduct(brand_id=brand_id, key=it["key"])
        row.title, row.url, row.image = it["title"], it["url"], it["image"]
        row.site_price, row.currency, row.in_stock = it["site_price"], it["currency"], 1 if it["in_stock"] else 0
        row.ids, row.kind = it["ids"], it["kind"]
        extra = {}
        if sold[it["key"]].get("units"):
            extra = {"store_units": sold[it["key"]]["units"], "store_revenue": round(sold[it["key"]]["revenue"], 2),
                     "store_rank": rank[it["key"]], "store_window": store["sales_window"]}
        row.meta = {**meta.get(it["key"], {}), **extra}
        row.synced_at = now
        if row.price_usd in (None, 0) and it["currency"] == "USD" and it["site_price"]:
            row.price_usd = it["site_price"]  # a USD store's price is already the plan's price
        session.add(row)
        seen.add(it["key"])
    for k, row in keep.items():
        if k not in seen:
            session.delete(row)  # gone from the store
    session.commit()
    notes = " ".join(n for n in (meta_note, store["note"]) if n)
    return {"platform": store["platform"], "products": len(items), "with_meta_results": len(meta),
            "meta_products_not_on_site": unmatched, "note": notes,
            "categories": len(store["cats"]) or len(_categories_from_kinds(items)),
            "with_sales": len(order), "sales_window": store["sales_window"]}


def _categories_from_kinds(items: list[dict]) -> list[dict]:
    """WooCommerce (and stores without collections): the category names the products carry."""
    from collections import Counter
    counts = Counter(k.strip() for it in items for k in (it.get("kind") or "").split(",") if k.strip())
    return [{"name": n[:80], "count": c} for n, c in counts.most_common(40)]


def _cat_key(brand_id: int) -> str:
    return f"site_categories:{int(brand_id)}"


def save_site_categories(session: Session, brand_id: int, cats: list[dict]) -> None:
    import json

    from .db import Setting
    row = session.get(Setting, _cat_key(brand_id))
    val = json.dumps(cats)
    if row:
        row.value = val
    else:
        session.add(Setting(key=_cat_key(brand_id), value=val))


def site_categories(session: Session, brand_id: int) -> list[dict]:
    """[{name, niche, note}]: the store's own categories, each tied to the app category used for estimates."""
    import json

    from .db import Setting
    row = session.get(Setting, _cat_key(brand_id))
    try:
        cats = json.loads(row.value) if row and row.value else []
    except ValueError:
        cats = []
    default = store_default(session, brand_id)
    out = []
    for c in cats:
        g = guess_category(c["name"], "", default) or ("other", "")
        out.append({"name": c["name"], "niche": g[0], "count": c.get("count", 0)})
    return out


# ---------- category: from the product's own words, else the store's usual one ----------

_CELEB = re.compile(r"\b(inspired|movie|film|series|tv show|costume|cosplay|worn by|as seen|character|hero|villain)\b")
_JACKET = re.compile(r"\b(jackets?|coats?|blazers?|gilets?|trench|bomber|biker|racer|varsity|parka)\b")
_LEATHER = re.compile(r"\b(leather|suede|shearling|sheepskin|lambskin|cowhide)\b")
_RULES = [  # first match wins; aprons first so "chef apron" is not kitchenware
    (re.compile(r"\baprons?\b"), "other", None),
    (re.compile(r"\b(smart ?watch|phones?|chargers?|earbuds|headphones?|speakers?|gadgets?|cables?|power ?banks?)\b"),
     "electronics_gadgets", None),
    (re.compile(r"\b(bags?|wallets?|belts?|purses?|backpacks?|totes?|satchels?|holdalls?|briefcases?|card ?holders?|clutch)\b"),
     "bags_accessories", None),
    (re.compile(r"\b(shoes?|boots?|sneakers?|trainers?|sandals?|loafers?|slippers?|heels|footwear)\b"), "footwear", None),
    (re.compile(r"\b(rings?|necklaces?|bracelets?|earrings?|pendants?|watch(es)?|jewell?e?ry|bangles?)\b"), "jewelry_watches", None),
    (re.compile(r"\b(cutlery|knife|knives|forks?|spoons?|dinner ?set|cookware|kitchen|plates?|bowls?|mugs?|tableware)\b"),
     "kitchen_cutlery", None),
    (re.compile(r"\b(cushions?|rugs?|curtains?|lamps?|vases?|decor|throws?|bedding|wall art)\b"), "home_decor", None),
    (re.compile(r"\b(perfume|fragrance|serum|creams?|skin ?care|lipstick|make ?up|beauty)\b"), "beauty_skincare", None),
    (re.compile(r"\b(swim\w*|bikinis?|beach ?wear|board ?shorts)\b"), "swimwear_summer", None),
    (re.compile(r"\b(kids?|baby|babies|toys?|children)\b"), "kids_toys", None),
    (re.compile(r"\b(dogs?|cats?|pets?|leash|collars? for)\b"), "pets", None),
    (re.compile(r"\b(yoga|gym|fitness|dumbbells?|resistance band)\b"), "health_fitness", None),
    (re.compile(r"\b(shirts?|t-?shirts?|tees?|hoodies?|sweatshirts?|dress(es)?|trousers?|jeans|kurtas?|abayas?|scarf|scarves|caps?|hats?)\b"),
     "apparel", None),
]


def _apron_note(text: str) -> str:
    # the main material comes first: "Canvas Work Apron | Leather Straps" is a canvas apron
    if re.search(r"\b(canvas|denim|cotton|linen)\b", text):
        return "Canvas aprons" if "canvas" in text else "Aprons"
    return "Leather aprons" if _LEATHER.search(text) else "Aprons"


def guess_category(title: str, kind: str, store_default: tuple[str, str] | None = None) -> tuple[str, str] | None:
    """(category key, note) for a product, or None when even the store gives no hint.
    store_default: the (category, note) of the store's latest plan."""
    text = f"{title} {kind}".lower()
    default_niche = store_default[0] if store_default else ""
    if _JACKET.search(text) and not re.search(r"\baprons?\b", text):
        if _CELEB.search(text) or default_niche == "celebrity_jackets":
            return "celebrity_jackets", ""
        if _LEATHER.search(text) or default_niche == "leather_jackets":
            return "leather_jackets", ""
        return "apparel", ""
    for rx, niche, _ in _RULES:
        if rx.search(text):
            return (niche, _apron_note(text)) if niche == "other" else (niche, "")
    return store_default or None


def store_category(session: Session, brand_id: int) -> tuple[str, str] | None:
    """What a store mostly sells, for a blank New plan: its products' most common category, else its own name
    and web address ("apron.co.uk" sells aprons), else the category of its latest plan."""
    from collections import Counter

    from . import brands
    rows = session.scalars(select(StoreProduct).where(StoreProduct.brand_id == brand_id)).all()
    own = {c["name"]: c["niche"] for c in site_categories(session, brand_id)}
    mine = Counter(p.kind for p in rows if p.in_stock and p.kind in own)
    if mine:  # the store's own category that holds most of its products
        name = mine.most_common(1)[0][0]
        return own[name], name
    counts = Counter(g for g in (guess_category(p.title, p.kind) for p in rows if p.in_stock) if g)
    if counts:
        return counts.most_common(1)[0][0]
    try:
        vals = brands.settings_for(session, brand_id)
        names = {b["id"]: b["name"] for b in brands.all_brands(session)}
    except brands.BrandError:
        return None
    host = re.sub(r"^https?://(www\.)?", "", vals.get("store_url", "") or "").split("/")[0]
    words = re.sub(r"[._-]+", " ", f"{names.get(brand_id, '')} {host}")
    # a web address is often one run-together word ("myapron", "trendyjackets"): look inside it too
    for hint in ("apron", "jacket", "cutlery", "knife", "shoe", "boot", "bag", "wallet", "jewel", "watch", "perfume", "toy"):
        if hint in host.lower() and hint not in words.lower().split():
            words += f" {hint}"
    return guess_category(words, "") or store_default(session, brand_id)


def store_default(session: Session, brand_id: int) -> tuple[str, str] | None:
    from .db import Plan
    p = session.scalars(select(Plan).where(Plan.brand_id == brand_id).order_by(Plan.id.desc()).limit(1)).first()
    return (p.niche, p.category_note or "") if p and p.niche else None


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
    rank, units = int(m.get("store_rank") or 0), int(m.get("store_units") or 0)
    if 0 < rank <= TOP_BEST:
        keep = f" You keep ${margin:.2f} per order." if margin is not None else ""
        ads = f"ads have only spent ${spend:.0f} on it" if spend else "it has not been advertised yet"
        return 700 + (margin or 0) + sales * 10 - rank, f"Best seller in your store (#{rank})", \
            f"{units} sold in your store ({m.get('store_window', '')}), and {ads}: a strong product to push.{keep}"
    if sales == 1:
        return 400 + (margin or 0), "One sale so far", \
            f"1 sale from ${spend:.0f} of ads. Promising: worth a proper test with its own budget."
    if clicks >= 20:
        return 60, "Clicks, no sales", \
            f"{clicks} clicks from ${spend:.0f} of ads but no sale. Check the product page, price and photos first."
    if spend >= 20:
        return 20, "Spent without results", f"${spend:.0f} of ads with almost no clicks: the ad or the product did not catch on."
    score = 300 + (margin if margin is not None else 0) + (10 if p.image else -50) + (60 - rank if rank else 0)
    keep = f"You keep ${margin:.2f} per order" if margin is not None else "Enter its costs to see the margin"
    if units:
        keep += f". {units} sold in your store (#{rank})"
    if spend > 0 or clicks > 0:
        return score, "Barely tested", \
            f"Only ${spend:.0f} of ads and {clicks} clicks so far: too little to judge. {keep}."
    return score, "Not tested yet", f"Never advertised. {keep}."


def ranked(session: Session, brand_id: int) -> list[dict]:
    rows = session.scalars(select(StoreProduct).where(StoreProduct.brand_id == brand_id)).all()
    default = store_default(session, brand_id)
    own = {c["name"] for c in site_categories(session, brand_id)}
    out = []
    for p in rows:
        score, verdict, reason = assess(p)
        cat = guess_category(p.title, p.kind, default)
        if cat and p.kind in own:
            cat = (cat[0], p.kind)  # the store's own category name is the note the planner sees
        out.append({"key": p.key, "title": p.title, "url": p.url, "image": p.image, "site_price": p.site_price,
                    "currency": p.currency, "in_stock": bool(p.in_stock), "kind": p.kind,
                    "price_usd": p.price_usd or 0, "cost_usd": p.cost_usd or 0, "shipping_usd": p.shipping_usd or 0,
                    "meta": p.meta or {}, "score": round(score, 2), "verdict": verdict, "reason": reason,
                    "niche": cat[0] if cat else "", "category_note": cat[1] if cat else "",
                    "site_category": p.kind if p.kind in own else "",
                    "synced_at": p.synced_at.isoformat() if p.synced_at else ""})
    out.sort(key=lambda x: -x["score"])
    return out
