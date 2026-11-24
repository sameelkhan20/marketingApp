"""Cash on delivery for the copy, and the store-link check before launch."""

from backend import storecheck


def test_plan_keeps_cod_for_the_copy(client, auth):
    body = {"budget_pkr": 300000, "country": "PK", "product": "Cutlery set", "niche": "kitchen_cutlery",
            "price_usd": 30, "cod": True}
    p = client.post("/api/plans", json=body, headers=auth).json()
    assert p["inputs"]["cod"] is True and "return_rate_pct" not in p["estimate"]["assumptions"]


class _R:
    def __init__(self, status, text="", url="https://shop.test/p", headers=None):
        self.status_code, self.text, self.url, self.headers = status, text, url, headers or {}


def test_store_link_check(monkeypatch):
    monkeypatch.setattr(storecheck, "ENABLED", True)
    monkeypatch.setattr(storecheck.requests, "get", lambda *a, **k: _R(404))
    assert storecheck.check("https://shop.test/p")[0]["hard"]
    monkeypatch.setattr(storecheck.requests, "get", lambda *a, **k: _R(403))
    assert not storecheck.check("https://shop.test/p")[0]["hard"]  # bot protection is not a broken link
    monkeypatch.setattr(storecheck.requests, "get", lambda *a, **k: _R(200, "<script>fbq('init')</script>"))
    assert storecheck.check("https://shop.test/p")[0]["ok"]
    monkeypatch.setattr(storecheck.requests, "get", lambda *a, **k: _R(200, "<html>woo</html>"))
    assert any(c["key"] == "store_pixel" for c in storecheck.check("https://shop.test/p"))
    monkeypatch.setattr(storecheck.requests, "get", lambda *a, **k: _R(200, '<link href="//cdn.shopify.com/x">'))
    assert storecheck.check("https://shop.test/p")[0]["ok"]  # Shopify's pixel is not in the page source
