"""Products: read from the store's site, joined with their Meta ad results, ranked; plans carry the product page."""

import json

import pytest

from backend import products
from backend.db import Plan, StoreProduct, get_session


class _R:
    def __init__(self, status, body, ctype="application/json"):
        self.status_code, self.headers = status, {"content-type": ctype}
        self.text = body if isinstance(body, str) else json.dumps(body)
        self._b = body

    def json(self):
        if isinstance(self._b, str):
            raise ValueError("not json")
        return self._b


SHOP = {"products": [
    {"id": 1, "title": "Blacksmith & Forge Apron", "handle": "forge", "product_type": "Aprons",
     "variants": [{"id": 48123065630890, "price": "89.00", "available": True, "sku": "F1"}],
     "images": [{"src": "https://cdn.test/forge.jpg"}]},
    {"id": 2, "title": "Waxed Canvas Apron", "handle": "canvas", "product_type": "Aprons",
     "variants": [{"id": 48123065761962, "price": "45.00", "available": True}], "images": []},
    {"id": 3, "title": "Sold Out Apron", "handle": "gone", "variants": [{"id": 9, "price": "30", "available": False}]},
]}


def _shopify_site(url, **kw):
    if "/products.json" in url:
        return _R(200, SHOP if "page=1" in url else {"products": []})
    return _R(404, "<html></html>", "text/html")


def test_reads_shopify_and_woocommerce(monkeypatch):
    monkeypatch.setattr(products.requests, "get", _shopify_site)
    platform, items = products.read_site("https://apron.test/some/page")
    assert platform == "shopify" and len(items) == 3
    forge = items[0]
    assert forge["url"] == "https://apron.test/products/forge" and forge["site_price"] == 89.0
    assert "48123065630890" in forge["ids"] and items[2]["in_stock"] is False

    woo = [{"id": 7, "name": "Leather Apron", "permalink": "https://w.test/p/7", "sku": "LA",
            "prices": {"price": "4500", "currency_code": "GBP", "currency_minor_unit": 2}, "is_in_stock": True,
            "images": [{"src": "https://w.test/a.jpg"}], "categories": [{"name": "Aprons"}]}]

    def woo_site(url, **kw):
        if "/products.json" in url:
            return _R(404, "<html>not shopify</html>", "text/html")
        return _R(200, woo if "page=1" in url else [])
    monkeypatch.setattr(products.requests, "get", woo_site)
    platform, items = products.read_site("https://w.test")
    assert platform == "woocommerce" and items[0]["site_price"] == 45.0 and items[0]["currency"] == "GBP"
    assert "wc_post_id_7" in items[0]["ids"]


def test_bot_protection_gives_a_clear_message(monkeypatch):
    monkeypatch.setattr(products.requests, "get",
                        lambda url, **kw: _R(403, "<title>Just a moment...</title>", "text/html"))
    with pytest.raises(products.ProductError, match="bot protection"):
        products.read_site("https://apron.test")


def test_meta_rows_match_site_products():
    _, items = None, [
        {"key": "shopify:1", "title": "Blacksmith & Forge Apron", "ids": ["1", "48123065630890"]},
        {"key": "shopify:2", "title": "Waxed Canvas Apron", "ids": ["2", "48123065761962"]}]
    rows = [
        {"product_id": "48123065630890, Dark Brown Leather Blacksmith & Forge Apron", "spend": "2800",
         "inline_link_clicks": "15", "actions": [{"action_type": "purchase", "value": "2"}]},
        {"product_id": "48123065630890, Old name", "spend": "280", "inline_link_clicks": "5"},
        {"product_id": "99999, Waxed Canvas Apron", "spend": "560", "inline_link_clicks": "3"},   # by name
        {"product_id": "27159426673746869", "spend": "1000"},                                     # catalogue id only
    ]
    by_id = products.meta_by_product(rows, "PKR")
    meta, unmatched = products.match(items, by_id)
    assert meta["shopify:1"]["purchases"] == 2 and meta["shopify:1"]["clicks"] == 20
    assert meta["shopify:1"]["spend_usd"] == round((2800 + 280) / 280, 2)   # PKR at the test rate
    assert meta["shopify:2"]["clicks"] == 3 and unmatched == 1


def test_ranking_explains_itself():
    def p(**kw):
        base = dict(in_stock=1, price_usd=0, cost_usd=0, shipping_usd=0, meta={}, image="x")
        base.update(kw)
        return StoreProduct(**base)
    proven = products.assess(p(price_usd=90, cost_usd=30, shipping_usd=10, meta={"spend_usd": 40, "purchases": 4}))
    untested = products.assess(p(price_usd=90, cost_usd=30, shipping_usd=10))
    clicks = products.assess(p(meta={"spend_usd": 50, "clicks": 40}))
    losing = products.assess(p(price_usd=20, cost_usd=25))
    gone = products.assess(p(in_stock=0))
    assert proven[1] == "Proven seller" and "$10.00 per sale" in proven[2]
    assert proven[0] > untested[0] > clicks[0] > losing[0] > gone[0]
    assert untested[1] == "Not tested yet" and "$50.00" in untested[2]


def test_sync_keeps_costs_and_plan_uses_product_page(client, auth, monkeypatch):
    from backend import settings_store
    monkeypatch.setattr(products.requests, "get", _shopify_site)
    with get_session() as s:
        settings_store.set_many(s, {"store_url": "https://apron.test"})
    r = client.post("/api/brands/0/products/sync", headers=auth)
    assert r.status_code == 200 and r.json()["products"] == 3 and r.json()["platform"] == "shopify"
    key = "shopify:1"
    saved = client.post(f"/api/products/0/costs?key={key}", json={"price_usd": 110, "cost_usd": 30, "shipping_usd": 15},
                        headers=auth).json()
    assert saved["verdict"] == "Not tested yet" and "$65.00" in saved["reason"]
    client.post("/api/brands/0/products/sync", headers=auth)          # reading again keeps the owner's numbers
    rows = client.get("/api/products?brand_id=0", headers=auth).json()
    forge = next(x for x in rows if x["key"] == key)
    assert forge["price_usd"] == 110 and forge["cost_usd"] == 30 and rows[-1]["verdict"] == "Out of stock"

    body = {"budget_pkr": 100000, "country": "GB", "product": forge["title"], "niche": "other",
            "category_note": "leather aprons", "price_usd": 110, "landing_url": forge["url"]}
    plan = client.post("/api/plans", json=body, headers=auth).json()
    assert plan["inputs"]["landing_url"] == "https://apron.test/products/forge"
    from backend import brands
    with get_session() as s:
        p = s.get(Plan, plan["id"])
        assert brands.with_landing(brands.settings_for(s, 0), p)["store_url"] == "https://apron.test/products/forge"
        s.query(StoreProduct).delete(); s.commit()
        settings_store.set_many(s, {"store_url": ""})
    bad = dict(body, landing_url="not a link")
    assert client.post("/api/plans", json=bad, headers=auth).status_code == 422


def test_team_members_cannot_read_products(client, auth):
    client.post("/api/users/register", json={"email": "prodstaff@example.com", "password": "StaffPass9"}, headers=auth)
    tok = client.post("/api/auth/login", json={"email": "prodstaff@example.com", "password": "StaffPass9"}).json()
    h = {"Authorization": f"Bearer {tok['access_token']}"}
    assert client.post("/api/brands/0/products/sync", headers=h).status_code == 403
    assert client.get("/api/products?brand_id=0", headers=h).status_code == 200
