"""Refunds and COD refusals in the profit maths, and the store-link check before launch."""

from backend import storecheck
from backend.estimator import estimate_orders


def _e(country, **kw):
    return estimate_orders(500000, country, "leather_jackets", 150, 30, unit_cost_usd=40, shipping_usd=20, **kw)


def test_refunds_abroad_lower_the_break_even():
    plain, refunds = _e("US"), _e("US", return_rate_pct=20)
    assert plain.break_even_cpa_usd == 90.0
    assert refunds.break_even_cpa_usd == 60.0  # 0.8 x 150 - 40 - 20: the product and shipping are spent anyway
    assert any("refunded or returned" in w for w in refunds.warnings)


def test_cod_only_counts_in_pakistan():
    assert _e("US", cod=True).assumptions["return_rate_pct"] == 0          # COD means nothing abroad
    pk = _e("PK", cod=True)
    assert pk.assumptions["return_rate_assumed"] and pk.assumptions["return_rate_pct"] == 20.0
    assert pk.break_even_cpa_usd == 64.0  # 0.8 x (150 - 40) - 20 x 1.2: the refused parcel comes back


def test_plan_keeps_the_brief(client, auth):
    body = {"budget_pkr": 300000, "country": "GB", "product": "Biker jacket", "niche": "leather_jackets",
            "price_usd": 150, "return_rate_pct": 15, "unit_cost_usd": 40, "shipping_usd": 20}
    p = client.post("/api/plans", json=body, headers=auth).json()
    assert p["inputs"]["return_rate_pct"] == 15 and p["inputs"]["cod"] is False
    assert p["estimate"]["assumptions"]["return_rate_pct"] == 15.0


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
