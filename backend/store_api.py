"""Read a store through its own API, with keys the owner creates (read-only):

Shopify: the store's .myshopify.com address and an Admin API access token (a custom app with the
read_products and read_orders scopes). Gives products, stock, collections and orders (real sales).
WooCommerce: a REST API consumer key and secret with Read permission. Gives products, stock, categories
and the top-sellers report (real sales).

Every call here only reads. Nothing on the store is ever changed.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from urllib.parse import urlsplit

import requests

SHOPIFY_API = "2025-01"
TIMEOUT = 30
MAX_PAGES = 40           # 40 x 250 Shopify rows, 40 x 100 WooCommerce rows
MAX_COLLECTIONS = 40
SALES_DAYS = 365
NOT_CATEGORIES = {"frontpage", "home", "all", "all-products", "products", "new-in", "new", "new-arrivals", "featured",
                  "sale", "shop", "catalog", "best-selling", "best-sellers", "bestsellers", "uncategorized"}


class StoreAPIError(Exception):
    pass


def platform_of(vals: dict) -> str:
    """Which keys this store has: "shopify", "woocommerce", "site" (its own API) or ""."""
    if vals.get("shop_domain") and vals.get("shop_token"):
        return "shopify"
    if vals.get("woo_key") and vals.get("woo_secret"):
        return "woocommerce"
    if vals.get("site_api_key"):
        return "site"
    return ""


def _fail(r: requests.Response, who: str) -> None:
    if r.status_code in (401, 403) and "Just a moment" not in r.text[:2000]:
        raise StoreAPIError(f"{who} refused the keys ({r.status_code}): check them, and that they may read products"
                            " (and orders).")
    if "Just a moment" in r.text[:2000]:
        raise StoreAPIError(f"The store's bot protection (Cloudflare) blocked the {who} API. Allow the server through "
                            "it, or turn the challenge off for /wp-json/.")
    if r.status_code == 404:
        raise StoreAPIError(f"{who} API not found at this address ({r.status_code}): check the store address.")
    if r.status_code >= 400:
        raise StoreAPIError(f"{who} API error {r.status_code}.")


# ---------- Shopify ----------

def _shop_domain(v: str) -> str:
    v = re.sub(r"^https?://", "", (v or "").strip()).split("/")[0].lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*\.myshopify\.com", v):
        raise StoreAPIError("The Shopify address must be the store's .myshopify.com address (like my-store.myshopify.com).")
    return v


class Shopify:
    def __init__(self, domain: str, token: str):
        self.base = f"https://{_shop_domain(domain)}/admin/api/{SHOPIFY_API}"
        self.s = requests.Session()
        self.s.headers.update({"X-Shopify-Access-Token": token.strip(), "Accept": "application/json"})

    def get(self, path: str, **params) -> requests.Response:
        try:
            r = self.s.get(f"{self.base}/{path}", params=params, timeout=TIMEOUT)
        except requests.RequestException as ex:
            raise StoreAPIError(f"Could not reach Shopify ({type(ex).__name__}).")
        _fail(r, "Shopify")
        return r

    def all(self, path: str, key: str, **params) -> list[dict]:
        out, r = [], self.get(path, limit=250, **params)
        for _ in range(MAX_PAGES):
            out += r.json().get(key) or []
            nxt = r.links.get("next", {}).get("url")
            if not nxt:
                break
            try:
                r = self.s.get(nxt, timeout=TIMEOUT)
            except requests.RequestException as ex:
                raise StoreAPIError(f"Could not reach Shopify ({type(ex).__name__}).")
            _fail(r, "Shopify")
        return out


def read_shopify(domain: str, token: str) -> dict:
    api = Shopify(domain, token)
    shop = api.get("shop.json").json().get("shop") or {}
    currency = (shop.get("currency") or "")[:3]
    site = shop.get("domain") or shop.get("myshopify_domain") or ""
    products = api.all("products.json", "products", status="active",
                       fields="id,title,handle,product_type,variants,images")
    items = []
    for p in products:
        variants = p.get("variants") or []
        prices = [float(v["price"]) for v in variants if v.get("price") not in (None, "")]
        stocked = any((v.get("inventory_quantity") or 0) > 0 or v.get("inventory_policy") == "continue"
                      or not v.get("inventory_management") for v in variants) if variants else True
        ids = [str(p.get("id"))] + [str(v.get("id")) for v in variants] + [str(v["sku"]) for v in variants if v.get("sku")]
        items.append({"key": f"shopify:{p.get('id')}", "pid": str(p.get("id")), "title": (p.get("title") or "")[:200],
                      "url": f"https://{site}/products/{p.get('handle', '')}" if site else "",
                      "image": ((p.get("images") or [{}])[0].get("src") or "")[:500],
                      "site_price": min(prices) if prices else 0.0, "currency": currency, "in_stock": stocked,
                      "ids": [i for i in ids if i and i != "None"], "kind": (p.get("product_type") or "")[:80]})

    # categories: the store's collections, with the products in each
    cats = []
    colls = api.all("custom_collections.json", "custom_collections", fields="id,title,handle") + \
        api.all("smart_collections.json", "smart_collections", fields="id,title,handle")
    for c in colls[:MAX_COLLECTIONS]:
        if (c.get("handle") or "").lower() in NOT_CATEGORIES:
            continue
        members = api.all(f"collections/{c['id']}/products.json", "products", fields="id")
        pids = {str(m.get("id")) for m in members}
        if pids:
            cats.append({"name": (c.get("title") or "")[:80], "pids": pids})

    # real sales: units and revenue per product from the orders (cancelled and refunded ones left out)
    sales, note = {}, ""
    since = (date.today() - timedelta(days=SALES_DAYS)).isoformat()
    try:
        orders = api.all("orders.json", "orders", status="any", created_at_min=since,
                         fields="created_at,cancelled_at,financial_status,line_items")
        first = min((o.get("created_at", "")[:10] for o in orders), default="")
        for o in orders:
            if o.get("cancelled_at") or o.get("financial_status") in ("refunded", "voided"):
                continue
            for li in o.get("line_items") or []:
                pid = str(li.get("product_id") or "")
                if not pid:
                    continue
                a = sales.setdefault(pid, {"units": 0, "revenue": 0.0})
                a["units"] += int(li.get("quantity") or 0)
                a["revenue"] += float(li.get("price") or 0) * int(li.get("quantity") or 0)
        if first and first > (date.today() - timedelta(days=90)).isoformat() and len(orders) >= 1:
            note = (f"Shopify gave orders from {first} on: without the read_all_orders scope it shares only the "
                    "last 60 days of orders.")
    except StoreAPIError as ex:
        note = f"Orders could not be read ({ex}) — add the read_orders scope to see real sales."
    return {"platform": "shopify", "currency": currency, "items": items, "cats": cats, "sales": sales,
            "sales_window": f"last {SALES_DAYS} days", "note": note}


# ---------- WooCommerce ----------

class Woo:
    def __init__(self, store_url: str, key: str, secret: str):
        u = urlsplit((store_url or "").strip())
        if u.scheme != "https" or not u.netloc:
            raise StoreAPIError("WooCommerce keys need the store URL as an https:// address (set it above).")
        self.base = f"https://{u.netloc}/wp-json/wc/v3"
        self.s = requests.Session()
        self.s.auth = (key.strip(), secret.strip())
        self.s.headers.update({"Accept": "application/json", "User-Agent": "AdsPlanner (read-only)"})

    def get(self, path: str, **params):
        try:
            r = self.s.get(f"{self.base}/{path}", params=params, timeout=TIMEOUT)
        except requests.RequestException as ex:
            raise StoreAPIError(f"Could not reach WooCommerce ({type(ex).__name__}).")
        _fail(r, "WooCommerce")
        try:
            return r.json(), r
        except ValueError:
            raise StoreAPIError("WooCommerce answered with a web page instead of data: check the store URL "
                                "and that its REST API is on.")

    def all(self, path: str, **params) -> list[dict]:
        out = []
        for page in range(1, MAX_PAGES + 1):
            data, r = self.get(path, per_page=100, page=page, **params)
            out += data if isinstance(data, list) else []
            if page >= int(r.headers.get("X-WP-TotalPages", "1") or 1):
                break
        return out


def read_woo(store_url: str, key: str, secret: str) -> dict:
    api = Woo(store_url, key, secret)
    try:
        currency = (api.get("data/currencies/current")[0].get("code") or "")[:3]
    except StoreAPIError:
        currency = ""
    items, cat_members = [], {}
    for p in api.all("products", status="publish"):
        try:
            price = float(p.get("price") or 0)
        except (TypeError, ValueError):
            price = 0.0
        pid = str(p.get("id"))
        names = [c.get("name", "") for c in (p.get("categories") or [])
                 if (c.get("slug") or "").lower() not in NOT_CATEGORIES]
        for n in names:
            cat_members.setdefault(n, set()).add(pid)
        ids = [pid, f"wc_post_id_{pid}"] + ([str(p["sku"]), f"{p['sku']}_{pid}"] if p.get("sku") else []) + \
            [str(v) for v in (p.get("variations") or [])]
        items.append({"key": f"woo:{pid}", "pid": pid, "title": re.sub(r"<[^>]+>", "", p.get("name") or "")[:200],
                      "url": (p.get("permalink") or "")[:500],
                      "image": ((p.get("images") or [{}])[0].get("src") or "")[:500],
                      "site_price": price, "currency": currency,
                      "in_stock": (p.get("stock_status") or "instock") != "outofstock",
                      "ids": ids, "kind": names[0][:80] if names else "", "total_sales": int(p.get("total_sales") or 0)})
    cats = [{"name": n[:80], "pids": m} for n, m in cat_members.items()]

    # real sales: the top-sellers report for the last year, else the lifetime count on each product
    sales, window, note = {}, f"last {SALES_DAYS} days", ""
    try:
        until = date.today()
        report, _ = api.get("reports/top_sellers", date_min=(until - timedelta(days=SALES_DAYS)).isoformat(),
                            date_max=until.isoformat())
        for row in report if isinstance(report, list) else []:
            pid = str(row.get("product_id") or "")
            if pid:
                sales[pid] = {"units": int(row.get("quantity") or 0), "revenue": 0.0}
    except StoreAPIError:
        pass
    if not sales:
        sales = {i["pid"]: {"units": i["total_sales"], "revenue": 0.0} for i in items if i["total_sales"]}
        window, note = "all time", "Sales are the all-time totals WooCommerce keeps on each product."
    return {"platform": "woocommerce", "currency": currency, "items": items, "cats": cats, "sales": sales,
            "sales_window": window, "note": note}


# ---------- the store's own site API (https://<store>/api/v1, Bearer key) ----------
# Lists answer {"data": [...], "meta": {...}}, paged with ?page=&limit=. Only GET calls are made here: the key
# can also change the store, and the app must never do that.

CUSTOM_PAGE = 500
CUSTOM_MAX_ORDER_DETAILS = 400   # orders whose list entry has no items are read one by one, up to this many
SKIP_ORDER = {"cancelled", "canceled", "refunded", "failed", "voided", "pending"}   # pending: not paid yet


def _first(d: dict, *keys, default=None):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return default


class CustomSite:
    def __init__(self, store_url: str, key: str):
        u = urlsplit((store_url or "").strip())
        if u.scheme != "https" or not u.netloc:
            raise StoreAPIError("The site API needs the store URL as an https:// address (set it above).")
        self.site = f"https://{u.netloc}"
        self.base = f"{self.site}/api/v1"
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {key.strip()}", "Accept": "application/json",
                               "User-Agent": "AdsPlanner (read-only)"})

    def get(self, path: str, **params):
        try:
            r = self.s.get(f"{self.base}/{path}", params=params, timeout=TIMEOUT)
        except requests.RequestException as ex:
            raise StoreAPIError(f"Could not reach the site API ({type(ex).__name__}).")
        if r.status_code == 403 and "Just a moment" not in r.text[:2000]:
            raise StoreAPIError(f"The site API key has no access to '{path.split('/')[0]}': give it read access to "
                                "products, collections and orders.")
        if r.status_code == 429:
            raise StoreAPIError("The site API asked to slow down (600 calls a minute): try again in a minute.")
        _fail(r, "The site")
        try:
            return r.json()
        except ValueError:
            raise StoreAPIError("The site API answered with a web page instead of data: check the store URL.")

    def all(self, path: str, **params) -> list[dict]:
        out = []
        for page in range(1, MAX_PAGES + 1):
            body = self.get(path, page=page, limit=CUSTOM_PAGE, **params)
            rows = body.get("data") if isinstance(body, dict) else body
            rows = rows if isinstance(rows, list) else []
            out += rows
            meta = body.get("meta") or {} if isinstance(body, dict) else {}
            last = _first(meta, "last_page", "pages", "total_pages")
            if len(rows) < CUSTOM_PAGE or (last and page >= int(last)):
                break
        return out


def _names(cats) -> list[str]:
    """Real category names: lists like "All Products", "Home page", "Best Selling" and empty names like "1" left out."""
    out = []
    for c in cats or []:
        n = _first(c, "name", "title", "slug") if isinstance(c, dict) else c
        slug = str(_first(c, "slug", default="") if isinstance(c, dict) else c).lower()
        name = str(n or "").strip()
        if not name or name.isdigit() or slug in NOT_CATEGORIES or name.lower().replace(" ", "-") in NOT_CATEGORIES:
            continue
        out.append(name)
    return out


def read_custom(store_url: str, key: str) -> dict:
    api = CustomSite(store_url, key)
    # collection slugs -> names, so products listing slugs show their real names
    cat_name = {}
    for c in api.all("categories"):
        name = _first(c, "name", "title")
        for k in (_first(c, "slug"), _first(c, "id")):
            if k not in (None, "") and name:
                cat_name[str(k)] = str(name)
    currency = ""
    try:
        st = api.get("settings")
        st = st.get("data", st) if isinstance(st, dict) else {}
        currency = str(_first(st, "currency", "currency_code", default="") or "")[:3].upper()
    except StoreAPIError:
        pass

    items, members = [], {}
    for p in api.all("products", status="active"):
        pid = str(_first(p, "id", default=""))
        if not pid:
            continue
        try:
            price = float(_first(p, "price", "sale_price", default=0) or 0)
        except (TypeError, ValueError):
            price = 0.0
        imgs = _first(p, "images", default=[]) or []
        img = (_first(imgs[0], "url", "src", default="") if imgs and isinstance(imgs[0], dict)
               else (imgs[0] if imgs else _first(p, "image", "image_url", default="")))
        slug = _first(p, "slug", "handle", default="")
        url = _first(p, "url", "permalink", default="") or (f"{api.site}/p/{slug}" if slug else "")
        stock, tracked = _first(p, "stock"), p.get("track_stock")
        sold_out = bool(p.get("sold_out")) or (bool(tracked) and stock is not None and int(stock or 0) <= 0)
        names = [cat_name.get(str(n), str(n)) for n in _names(_first(p, "categories", "collections", default=[]))]
        for n in names:
            members.setdefault(n, set()).add(pid)
        sku = _first(p, "sku", default="")
        items.append({"key": f"site:{pid}", "pid": pid, "title": str(_first(p, "name", "title", default=""))[:200],
                      "url": str(url)[:500], "image": str(img or "")[:500], "site_price": price,
                      "currency": currency, "in_stock": not sold_out,
                      "ids": [i for i in (pid, str(sku) if sku else "") if i],
                      "kind": names[0][:80] if names else ""})
    cats = [{"name": n[:80], "pids": m} for n, m in members.items()]

    # real sales from the orders of the last year (cancelled and refunded ones left out)
    sales, note = {}, ""
    since = (date.today() - timedelta(days=SALES_DAYS)).isoformat()
    by_sku = {i["ids"][1]: i["pid"] for i in items if len(i["ids"]) > 1}
    try:
        orders = api.all("orders", since=since)
        details_read = 0
        for o in orders:
            if str(_first(o, "status", default="")).lower() in SKIP_ORDER:
                continue
            lines = _first(o, "items", "line_items", "lines", default=None)
            if lines is None and details_read < CUSTOM_MAX_ORDER_DETAILS and _first(o, "id") is not None:
                full = api.get(f"orders/{o['id']}")
                full = full.get("data", full) if isinstance(full, dict) else {}
                lines = _first(full, "items", "line_items", "lines", default=[])
                details_read += 1
            for li in lines or []:
                pid = str(_first(li, "product_id", default="") or by_sku.get(str(_first(li, "sku", default="")), ""))
                if not pid:
                    continue
                qty = int(_first(li, "quantity", "qty", default=1) or 1)
                try:
                    each = float(_first(li, "price", "unit_price", default=0) or 0)
                except (TypeError, ValueError):
                    each = 0.0
                a = sales.setdefault(pid, {"units": 0, "revenue": 0.0})
                a["units"] += qty
                a["revenue"] += each * qty
        if details_read >= CUSTOM_MAX_ORDER_DETAILS:
            note = f"Only the newest {CUSTOM_MAX_ORDER_DETAILS} orders were read in detail."
    except StoreAPIError as ex:
        note = f"Orders could not be read ({ex})."
    return {"platform": "site", "currency": currency, "items": items, "cats": cats, "sales": sales,
            "sales_window": f"last {SALES_DAYS} days", "note": note}


def read_store(vals: dict) -> dict:
    """The store's products, categories and real sales through its own API."""
    kind = platform_of(vals)
    if kind == "shopify":
        return read_shopify(vals["shop_domain"], vals["shop_token"])
    if kind == "woocommerce":
        return read_woo(vals.get("store_url", ""), vals["woo_key"], vals["woo_secret"])
    if kind == "site":
        return read_custom(vals.get("store_url", ""), vals["site_api_key"])
    raise StoreAPIError("Connect the store first: add its Shopify, WooCommerce or site API keys under Connections.")
