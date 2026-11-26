"""Make it sell, filled from the store's own ads and the product page; plus the owner's own notes."""

from backend import brief_suggest, llm, planner
from backend.estimator import estimate_orders, estimate_to_dict


def test_fill_from_ads_and_page(client, auth, monkeypatch):
    seen = {}
    monkeypatch.setattr(brief_suggest, "_ads_copy", lambda vals: ["SALE NOW ON | Full-grain leather with free custom branding"])
    monkeypatch.setattr(brief_suggest, "_page_facts", lambda url: {
        "title": "BBQ Apron", "description": "Material: Full-grain cowhide leather, 1.5 mm",
        "lines": ["Delivered in 5-7 working days", "30-day returns"]})
    monkeypatch.setattr(llm, "configured", lambda: True)

    def fake(system, prompt, schema, feature, **kw):
        seen["prompt"] = prompt
        return {"offer": "Free custom branding", "usp": "Full-grain cowhide, 1.5 mm", "delivery_days": 7,
                "returns": "30-day returns", "social_proof": "", "what_your_ads_do": "Lead with the sale and free branding."}
    monkeypatch.setattr(llm, "structured", fake)
    r = client.post("/api/brief/suggest", json={"brand_id": 0, "landing_url": "https://shop.test/p/bbq"}, headers=auth)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["offer"] == "Free custom branding" and out["delivery_days"] == 7 and out["social_proof"] == ""
    assert out["read"] == {"ads": 1, "page": True} and "free branding" in out["what_your_ads_do"]
    assert "free custom branding" in seen["prompt"] and "30-day returns" in seen["prompt"]


def test_fill_needs_something_to_read(client, auth, monkeypatch):
    monkeypatch.setattr(brief_suggest, "_ads_copy", lambda vals: [])
    monkeypatch.setattr(brief_suggest, "_page_facts", lambda url: {})
    r = client.post("/api/brief/suggest", json={"brand_id": 0}, headers=auth)
    assert r.status_code == 400 and "Nothing to read" in r.json()["detail"]
    assert client.post("/api/brief/suggest", json={"brand_id": 0, "landing_url": "nope"}, headers=auth).status_code == 400


def test_bad_ai_numbers_are_dropped(monkeypatch):
    from backend.db import get_session
    monkeypatch.setattr(brief_suggest, "_ads_copy", lambda vals: ["An ad"])
    monkeypatch.setattr(brief_suggest, "_page_facts", lambda url: {})
    monkeypatch.setattr(llm, "configured", lambda: True)
    monkeypatch.setattr(llm, "structured", lambda *a, **k: {"offer": "x" * 500, "usp": "", "delivery_days": 400,
                                                            "returns": "", "social_proof": "", "what_your_ads_do": ""})
    with get_session() as s:
        out = brief_suggest.suggest(s, 0)
    assert out["delivery_days"] == 0 and len(out["offer"]) == 200


def test_anything_else_reaches_the_plan(client, auth):
    body = {"budget_pkr": 100000, "country": "US", "product": "Leather apron", "niche": "other",
            "category_note": "leather aprons", "price_usd": 80, "extra": "New tan colour launches in December"}
    p = client.post("/api/plans", json=body, headers=auth).json()
    assert p["inputs"]["extra"] == "New tan colour launches in December"
    text = planner._user_prompt({**body, "duration_days": 30}, estimate_to_dict(estimate_orders(100000, "US")), [], [])
    assert "Anything else from the owner: New tan colour launches in December" in text
