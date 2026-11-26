"""Products: read through the store's own API (Shopify or WooCommerce keys), joined with their Meta ad results
and real sales, ranked; plans carry the product page."""

import json

import pytest

from backend import products, store_api
from backend.db import Plan, StoreProduct, get_session


class _R:
    def __init__(self, status, body, headers=None, links=None):
        self.status_code, self.headers, self.links = status, headers or {}, links or {}
        self.text = body if isinstance(body, str) else json.dumps(body)
        self._b = body

    def json(self):
        if isinstance(self._b, str):
            raise ValueError("not json")
        return self._b


class FakeShopify:
    """Shopify Admin API: a shop, three products (one sold out), a collection and orders."""
    PRODUCTS = [
        {"id": 1, "title": "Blacksmith & Forge Apron", "handle": "forge", "product_type": "Aprons",
         "variants": [{"id": 48123065630890, "price": "89.00", "sku": "F1", "inventory_quantity": 5,
                       "inventory_management": "shopify"}], "images": [{"src": "https://cdn.test/forge.jpg"}]},
        {"id": 2, "title": "Waxed Canvas Apron", "handle": "canvas", "product_type": "Aprons",
         "variants": [{"id": 48123065761962, "price": "45.00", "inventory_quantity": 2,
                       "inventory_management": "shopify"}], "images": []},
        {"id": 3, "title": "Sold Out Apron", "handle": "gone",
         "variants": [{"id": 9, "price": "30", "inventory_quantity": 0, "inventory_management": "shopify"}]},
    ]
    seen = []

    def __init__(self):
        self.headers = {}

    def get(self, url, params=None, timeout=None):
        FakeShopify.seen.append((url, dict(self.headers)))
        path = url.split("/admin/api/")[-1].split("/", 1)[-1]
        if path == "shop.json":
            return _R(200, {"shop": {"currency": "USD", "domain": "apron.test"}})
        if path == "products.json":
            return _R(200, {"products": self.PRODUCTS})
        if path == "custom_collections.json":
            return _R(200, {"custom_collections": [{"id": 77, "title": "Blacksmith Apron", "handle": "blacksmith-apron"},
                                                   {"id": 78, "title": "Frontpage", "handle": "frontpage"}]})
        if path == "smart_collections.json":
            return _R(200, {"smart_collections": []})
        if path == "collections/77/products.json":
            return _R(200, {"products": [{"id": 1}]})
        if path == "orders.json":
            return _R(200, {"orders": [
                {"created_at": "2026-01-10T10:00:00Z", "financial_status": "paid",
                 "line_items": [{"product_id": 2, "quantity": 3, "price": "45.00"}, {"product_id": 1, "quantity": 1, "price": "89"}]},
                {"created_at": "2026-02-10T10:00:00Z", "financial_status": "refunded",
                 "line_items": [{"product_id": 1, "quantity": 5, "price": "89"}]},           # refunded: not a sale
            ]})
        return _R(404, "<html></html>")


@pytest.fixture
def shopify(monkeypatch):
    FakeShopify.seen = []
    monkeypatch.setattr(store_api.requests, "Session", FakeShopify)
    return FakeShopify


def test_reads_shopify_through_its_api(shopify):
    got = store_api.read_store({"shop_domain": "https://my-store.myshopify.com/admin", "shop_token": "shpat_x"})
    assert got["platform"] == "shopify" and got["currency"] == "USD" and len(got["items"]) == 3
    forge = got["items"][0]
    assert forge["url"] == "https://apron.test/products/forge" and forge["site_price"] == 89.0
    assert "48123065630890" in forge["ids"] and got["items"][2]["in_stock"] is False
    assert got["cats"] == [{"name": "Blacksmith Apron", "pids": {"1"}}]           # "Frontpage" is not a category
    assert got["sales"] == {"2": {"units": 3, "revenue": 135.0}, "1": {"units": 1, "revenue": 89.0}}
    url, headers = shopify.seen[0]
    assert url.startswith("https://my-store.myshopify.com/admin/api/") and headers["X-Shopify-Access-Token"] == "shpat_x"


def test_shopify_address_must_be_myshopify():
    with pytest.raises(store_api.StoreAPIError, match="myshopify"):
        store_api.read_store({"shop_domain": "myapron.co.uk", "shop_token": "shpat_x"})
    with pytest.raises(store_api.StoreAPIError, match="Connect the store"):
        store_api.read_store({})


def test_reads_woocommerce_through_its_api(monkeypatch):
    class FakeWoo:
        def __init__(self):
            self.headers, self.auth = {}, None

        def get(self, url, params=None, timeout=None):
            assert self.auth == ("ck_1", "cs_1")
            path = url.split("/wp-json/wc/v3/")[-1]
            if path == "data/currencies/current":
                return _R(200, {"code": "GBP"})
            if path == "products":
                return _R(200, [{"id": 7, "name": "Leather Apron", "permalink": "https://w.test/p/7", "sku": "LA",
                                 "price": "45.00", "stock_status": "instock", "total_sales": 12,
                                 "categories": [{"name": "Chef Apron", "slug": "chef-apron"},
                                                {"name": "Uncategorized", "slug": "uncategorized"}],
                                 "images": [{"src": "https://w.test/a.jpg"}], "variations": [71]}],
                          headers={"X-WP-TotalPages": "1"})
            if path == "reports/top_sellers":
                return _R(200, [{"product_id": 7, "quantity": 9}])
            return _R(404, "<html></html>")
    monkeypatch.setattr(store_api.requests, "Session", FakeWoo)
    got = store_api.read_store({"store_url": "https://w.test/shop", "woo_key": "ck_1", "woo_secret": "cs_1"})
    it = got["items"][0]
    assert got["platform"] == "woocommerce" and got["currency"] == "GBP" and it["site_price"] == 45.0
    assert it["kind"] == "Chef Apron" and "wc_post_id_7" in it["ids"] and "71" in it["ids"]
    assert got["cats"] == [{"name": "Chef Apron", "pids": {"7"}}] and got["sales"]["7"]["units"] == 9


def test_cloudflare_and_wrong_keys_give_clear_messages(monkeypatch):
    class Blocked:
        def __init__(self):
            self.headers, self.auth = {}, None

        def get(self, url, **kw):
            return _R(403, "<title>Just a moment...</title>")
    monkeypatch.setattr(store_api.requests, "Session", Blocked)
    with pytest.raises(store_api.StoreAPIError, match="Cloudflare"):
        store_api.read_store({"store_url": "https://w.test", "woo_key": "ck", "woo_secret": "cs"})

    class Refused(Blocked):
        def get(self, url, **kw):
            return _R(401, {"code": "woocommerce_rest_cannot_view"})
    monkeypatch.setattr(store_api.requests, "Session", Refused)
    with pytest.raises(store_api.StoreAPIError, match="refused the keys"):
        store_api.read_store({"store_url": "https://w.test", "woo_key": "ck", "woo_secret": "cs"})


def test_sync_keeps_costs_ranks_real_sales_and_plan_uses_product_page(client, auth, shopify):
    from backend import settings_store
    with get_session() as s:
        settings_store.set_many(s, {"shop_domain": "my-store.myshopify.com", "shop_token": "shpat_x"})
    r = client.post("/api/brands/0/products/sync", headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["products"] == 3 and r.json()["platform"] == "shopify" and r.json()["with_sales"] == 2
    rows = {x["title"]: x for x in client.get("/api/products?brand_id=0", headers=auth).json()}
    canvas, forge = rows["Waxed Canvas Apron"], rows["Blacksmith & Forge Apron"]
    assert canvas["verdict"] == "Best seller in your store (#1)" and "3 sold" in canvas["reason"]
    assert canvas["price_usd"] == 45.0                       # a USD store's price is already the plan's price
    assert forge["site_category"] == "Blacksmith Apron"      # from its collection
    assert rows["Sold Out Apron"]["verdict"] == "Out of stock"

    saved = client.post(f"/api/products/0/costs?key={forge['key']}",
                        json={"price_usd": 110, "cost_usd": 30, "shipping_usd": 15}, headers=auth).json()
    assert "$65.00" in saved["reason"]
    client.post("/api/brands/0/products/sync", headers=auth)          # reading again keeps the owner's numbers
    again = {x["title"]: x for x in client.get("/api/products?brand_id=0", headers=auth).json()}
    assert again["Blacksmith & Forge Apron"]["cost_usd"] == 30

    body = {"budget_pkr": 100000, "country": "GB", "product": forge["title"], "niche": "other",
            "category_note": "leather aprons", "price_usd": 110, "landing_url": forge["url"]}
    plan = client.post("/api/plans", json=body, headers=auth).json()
    from backend import brands
    with get_session() as s:
        p = s.get(Plan, plan["id"])
        assert brands.with_landing(brands.settings_for(s, 0), p)["store_url"] == "https://apron.test/products/forge"
        s.query(StoreProduct).delete(); s.commit()
        settings_store.set_many(s, {"shop_domain": "", "shop_token": ""})
        products.save_site_categories(s, 0, []); s.commit()
    assert client.post("/api/plans", json=dict(body, landing_url="not a link"), headers=auth).status_code == 422


def test_no_keys_no_products(client, auth):
    r = client.post("/api/brands/0/products/sync", headers=auth)
    assert r.status_code == 400 and "keys" in r.json()["detail"]


def test_store_keys_are_encrypted_and_masked(client, auth):
    from backend.db import Setting
    r = client.post("/api/brands", json={"values": {"name": "Keys Store", "shop_domain": "k.myshopify.com",
                                                    "shop_token": "shpat_secret1", "woo_secret": "cs_secret2"}},
                    headers=auth)
    bid = r.json()["id"]
    out = client.get(f"/api/brands/{bid}", headers=auth).json()
    assert out["shop_token"] == "********" and out["woo_secret"] == "********" and out["shop_domain"] == "k.myshopify.com"
    with get_session() as s:
        raw = " ".join(x.value for x in s.query(Setting).all())
        assert "shpat_secret1" not in raw and "cs_secret2" not in raw
        from backend import brands
        assert brands.settings_for(s, bid)["shop_token"] == "shpat_secret1"
    client.post(f"/api/brands/{bid}", json={"values": {"shop_token": "********"}}, headers=auth)   # mask keeps it
    with get_session() as s:
        assert brands.settings_for(s, bid)["shop_token"] == "shpat_secret1"
    client.delete(f"/api/brands/{bid}", headers=auth)
    main = client.post("/api/meta/settings", json={"values": {"woo_key": "ck_main"}}, headers=auth).json()
    assert main["settings"]["woo_key"] == "********"
    client.post("/api/meta/settings", json={"values": {"woo_key": ""}}, headers=auth)


def test_only_the_top_5_real_sellers_are_called_best_sellers():
    def p(rank, units):
        return StoreProduct(in_stock=1, price_usd=0, cost_usd=0, shipping_usd=0, image="x",
                            meta={"store_rank": rank, "store_units": units, "store_window": "last 365 days"})
    top, ninth, plain = products.assess(p(1, 30)), products.assess(p(9, 2)), products.assess(p(0, 0))
    assert top[1] == "Best seller in your store (#1)" and "30 sold" in top[2]
    assert ninth[1] == "Not tested yet" and "2 sold in your store (#9)" in ninth[2]
    assert top[0] > ninth[0] > plain[0]


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


def test_team_members_cannot_read_products(client, auth):
    client.post("/api/users/register", json={"email": "prodstaff@example.com", "password": "StaffPass9"}, headers=auth)
    tok = client.post("/api/auth/login", json={"email": "prodstaff@example.com", "password": "StaffPass9"}).json()
    h = {"Authorization": f"Bearer {tok['access_token']}"}
    assert client.post("/api/brands/0/products/sync", headers=h).status_code == 403
    assert client.get("/api/products?brand_id=0", headers=h).status_code == 200


def test_category_is_guessed_from_the_product_and_the_store():
    g = products.guess_category
    assert g("Dark Brown Leather Blacksmith & Forge Apron", "") == ("other", "Leather aprons")
    assert g("The Wiltshire Waxed Canvas Apron", "Aprons") == ("other", "Canvas aprons")
    assert g("Chef Apron with pockets", "") == ("other", "Aprons")                 # not kitchenware
    assert g("Mens Black Leather Biker Jacket", "") == ("leather_jackets", "")
    assert g("Movie Inspired Brown Leather Jacket", "") == ("celebrity_jackets", "")
    # a jacket named after a film: the store's own category decides
    assert g("Top Gun Maverick Jacket", "", ("celebrity_jackets", "")) == ("celebrity_jackets", "")
    assert g("Classic Suede Bomber", "") == ("leather_jackets", "")
    assert g("Leather Wallet", "") == ("bags_accessories", "")
    assert g("24-piece cutlery set", "") == ("kitchen_cutlery", "")
    assert g("Mystery item", "", ("other", "Leather aprons")) == ("other", "Leather aprons")   # store's usual
    assert g("Mystery item", "") is None


def test_blank_plan_category_follows_the_store(client, auth):
    from backend import settings_store
    with get_session() as s:
        old = settings_store.get_all(s)["store_url"]
        settings_store.set_many(s, {"store_url": "https://apron.co.uk/"})
    got = client.get("/api/brands/0/category", headers=auth).json()
    assert (got["niche"], got["category_note"]) == ("other", "Aprons")
    with get_session() as s:
        s.add(StoreProduct(brand_id=0, key="x:1", title="Brown Leather Apron", in_stock=1, ids=[], meta={}))
        s.add(StoreProduct(brand_id=0, key="x:2", title="Black Leather Apron", in_stock=1, ids=[], meta={}))
        s.commit()
    assert client.get("/api/brands/0/category", headers=auth).json()["category_note"] == "Leather aprons"
    with get_session() as s:
        s.query(StoreProduct).delete(); s.commit()
        settings_store.set_many(s, {"store_url": old})


def test_meta_names_match_close_site_names():
    items = [{"key": "a", "title": "Dark Brown Full Grain Leather BBQ & Work Apron", "ids": []},
             {"key": "b", "title": "Black Full-Grain Leather Cross-Back Work Apron with Pockets", "ids": []}]
    by_id = {"4812": {"name": "Dark Brown Full-Grain Leather BBQ Work Apron", "spend_usd": 5, "impressions": 0,
                      "clicks": 2, "purchases": 1, "revenue_usd": 0},
             "9999": {"name": "Waxed Canvas Tote", "spend_usd": 1, "impressions": 0, "clicks": 0, "purchases": 0,
                      "revenue_usd": 0}}
    meta, unmatched = products.match(items, by_id)
    assert meta["a"]["purchases"] == 1 and "b" not in meta and unmatched == 1


def test_run_together_web_address_gives_the_category(client, auth):
    from backend import settings_store
    with get_session() as s:
        old = settings_store.get_all(s)["store_url"]
        settings_store.set_many(s, {"store_url": "https://myapron.co.uk/"})
    assert client.get("/api/brands/0/category", headers=auth).json()["niche"] == "other"
    with get_session() as s:
        settings_store.set_many(s, {"store_url": old})


def test_little_ad_spend_is_barely_tested_and_canvas_comes_first():
    p = StoreProduct(in_stock=1, price_usd=0, cost_usd=0, shipping_usd=0, image="x",
                     meta={"spend_usd": 11.0, "clicks": 12, "purchases": 0})
    verdict, reason = products.assess(p)[1:]
    assert verdict == "Barely tested" and "$11 of ads and 12 clicks" in reason
    assert products.guess_category("Charcoal Grey Canvas Work Apron | Leather Straps", "") == ("other", "Canvas aprons")
    assert products.guess_category("Vintage Wash Denim Overall Apron", "") == ("other", "Aprons")




class FakeSite:
    """The store's own /api/v1: products, collections, settings and orders (one without items in the list)."""
    calls = []

    def __init__(self):
        self.headers = {}

    def get(self, url, params=None, timeout=None):
        FakeSite.calls.append(("GET", url, dict(self.headers)))
        path = url.split("/api/v1/")[-1]
        page = (params or {}).get("page", 1)
        if page > 1:
            return _R(200, {"data": [], "meta": {}}, headers={"content-type": "application/json"})
        data = {
            "categories": {"data": [{"id": 1, "name": "Chef Apron", "slug": "chef-apron"},
                                    {"id": 2, "name": "All", "slug": "all"}]},
            "settings": {"data": {"currency": "usd"}},
            "products": {"data": [
                {"id": 12, "name": "Brown Chef Apron", "price": 64.99, "sku": "S&C00012", "slug": "brown-chef",
                 "images": [{"url": "https://myapron.test/a.jpg"}], "categories": ["chef-apron"], "track_stock": True, "stock": 8},
                {"id": 13, "name": "Sold Out Apron", "price": 50, "slug": "gone", "track_stock": True, "stock": 0,
                 "categories": [{"slug": "chef-apron", "name": "Chef Apron"}]}], "meta": {"last_page": 1}},
            "orders": {"data": [
                {"id": 900, "status": "paid", "items": [{"product_id": 12, "quantity": 2, "price": 64.99}]},
                {"id": 901, "status": "cancelled", "items": [{"product_id": 12, "quantity": 9, "price": 64.99}]},
                {"id": 902, "status": "shipped"}]},                      # no items in the list: read one by one
            "orders/902": {"data": {"id": 902, "items": [{"sku": "S&C00012", "qty": 1, "price": 64.99}]}},
        }.get(path)
        return _R(200, data, headers={"content-type": "application/json"}) if data else _R(404, "<html></html>")

    def post(self, *a, **k):
        raise AssertionError("the app must never write to the store")
    patch = delete = put = post


def test_reads_the_stores_own_site_api(monkeypatch):
    FakeSite.calls = []
    monkeypatch.setattr(store_api.requests, "Session", FakeSite)
    got = store_api.read_store({"store_url": "https://myapron.test/", "site_api_key": "key123"})
    assert got["platform"] == "site" and got["currency"] == "USD" and len(got["items"]) == 2
    apron, gone = got["items"]
    assert apron["url"] == "https://myapron.test/p/brown-chef" and apron["site_price"] == 64.99
    assert apron["kind"] == "Chef Apron" and "S&C00012" in apron["ids"] and apron["in_stock"]
    assert gone["in_stock"] is False                                  # stock 0 while counted: sold out
    assert got["cats"] == [{"name": "Chef Apron", "pids": {"12", "13"}}]
    sold = got["sales"]["12"]                                           # 2 paid + 1 found by its SKU; cancelled left out
    assert list(got["sales"]) == ["12"] and sold["units"] == 3 and round(sold["revenue"], 2) == 194.97
    assert all(m == "GET" for m, _, _ in FakeSite.calls)
    assert FakeSite.calls[0][1].startswith("https://myapron.test/api/v1/")
    assert FakeSite.calls[0][2]["Authorization"] == "Bearer key123"


def test_site_key_without_access_says_which_section(monkeypatch):
    class NoOrders(FakeSite):
        def get(self, url, params=None, timeout=None):
            if url.endswith("/products"):
                return _R(403, {"error": {"code": 403, "message": "no access"}})
            return super().get(url, params, timeout)
    monkeypatch.setattr(store_api.requests, "Session", NoOrders)
    with pytest.raises(store_api.StoreAPIError, match="no access to 'products'"):
        store_api.read_store({"store_url": "https://myapron.test/", "site_api_key": "k"})


def test_site_lists_are_not_categories():
    cats = [{"id": 2, "name": "All Products", "slug": "all-products"}, {"id": 11, "name": "1", "slug": "1"},
            {"id": 3, "name": "Home page", "slug": "frontpage"}, {"id": 5, "name": "Best Selling", "slug": "best-selling"},
            {"id": 7, "name": "Chef Apron", "slug": "chef-apron"}]
    assert store_api._names(cats) == ["Chef Apron"]
