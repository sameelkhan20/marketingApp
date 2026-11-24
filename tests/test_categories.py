"""Every product category: its own benchmarks, season, events and plan copy."""

from datetime import date

import pytest

from backend import planner, seasonality
from backend.benchmarks import CATEGORIES, price_multiplier
from backend.estimator import estimate_orders, estimate_to_dict


def test_every_category_is_complete():
    keys = {"label", "group", "cpm", "ctr", "cvr", "typical_price", "season", "tags", "noun", "audience", "hook",
            "risk", "guidance", "studio", "lifestyle", "image_note"}
    for k, c in CATEGORIES.items():
        assert keys <= set(c), k
        assert c["season"] in seasonality._DEMAND, k
        # celebrity hooks never carry the product name: it is usually the film's title
        assert ("{product}" in c["hook"]) != (k == "celebrity_jackets"), k


def test_price_is_judged_against_the_category():
    assert price_multiplier(60, 60) == 1.0          # a normal price for a 60 USD cutlery set
    assert price_multiplier(60, 150) > 1.0           # a cheap jacket
    assert price_multiplier(300, 60) < price_multiplier(300, 150)


def test_seasons_differ_by_category():
    june = date(2026, 6, 1)
    jackets = seasonality.season_factors("US", june, 30, "leather_jackets")
    swim = seasonality.season_factors("US", june, 30, "swimwear_summer")
    steady = seasonality.season_factors("US", june, 30, "kitchen_cutlery")
    assert jackets["label"] == "off season" and swim["cvr"] > steady["cvr"] > jackets["cvr"]
    # southern hemisphere flips weather seasons, not gifting
    assert seasonality.season_factors("AU", june, 30, "swimwear_summer")["cvr"] < swim["cvr"]


def test_events_only_lift_the_right_buyers():
    may = (date(2026, 5, 1), date(2026, 5, 31))
    cutlery = {e["name"]: e for e in seasonality.events("PK", *may, "kitchen_cutlery")}
    jackets = {e["name"]: e for e in seasonality.events("PK", *may, "leather_jackets")}
    assert "Mother's Day" in cutlery and cutlery["Mother's Day"]["cvr"] > 1
    assert "Week before Eid al-Adha" in cutlery          # hosting: kitchen and dining
    assert "Week before Eid al-Adha" not in jackets
    assert seasonality._easter(2026) == date(2026, 4, 5)
    gb = {e["name"] for e in seasonality.events("GB", date(2026, 3, 1), date(2026, 3, 31), "beauty_skincare")}
    assert "Mother's Day (Mothering Sunday)" in gb      # 3 weeks before Easter: 15 March 2026
    gulf = {e["name"] for e in seasonality.events("AE", date(2026, 11, 1), date(2026, 11, 30), "apparel")}
    assert "White Friday" in gulf and "Black Friday" not in gulf


def test_estimate_and_template_follow_the_category():
    e = estimate_orders(100000, "US", "kitchen_cutlery", 60, 30, start=date(2026, 6, 1))
    assert e.assumptions["niche"] == "kitchen_cutlery" and e.season["kind"] == "steady"
    assert not any("jacket" in w.lower() for w in e.warnings)
    inputs = {"budget_pkr": 100000, "country": "US", "product": "24-piece cutlery set", "niche": "kitchen_cutlery",
              "price_usd": 60, "duration_days": 30}
    plan = planner._template_plan(inputs, estimate_to_dict(e), [])
    text = str(plan).lower()
    assert "jacket" not in text and "cooking" in text and "24-piece cutlery set" in text
    assert all("product from the reference photo" in c["visual_prompt"] for c in plan["creatives"])


def test_other_category_needs_a_description(client, auth):
    body = {"budget_pkr": 100000, "country": "PK", "product": "Prayer mat", "niche": "other", "price_usd": 15}
    assert client.post("/api/estimate", json=body, headers=auth).status_code == 422
    ok = client.post("/api/estimate", json={**body, "category_note": "prayer mats"}, headers=auth)
    assert ok.status_code == 200
    p = client.post("/api/plans", json={**body, "category_note": "prayer mats"}, headers=auth).json()
    assert p["inputs"]["category_note"] == "prayer mats" and p["inputs"]["niche"] == "other"


def test_categories_listed_for_the_form(client, auth):
    m = client.get("/api/meta", headers=auth).json()
    keys = [c["key"] for c in m["categories"]]
    assert "kitchen_cutlery" in keys and "leather_jackets" in keys and keys[-1] == "other"


def test_no_video_formats():
    inputs = {"budget_pkr": 100000, "country": "US", "product": "Wallet", "niche": "bags_accessories",
              "price_usd": 40, "duration_days": 30}
    plan = planner._template_plan(inputs, estimate_to_dict(estimate_orders(100000, "US", "bags_accessories", 40)), [])
    formats = [c["format"] for c in plan["creatives"]]
    assert "Carousel" in formats and not any("video" in f.lower() for f in formats)
    plan["creatives"][1]["format"] = "Video (Reels/Stories)"
    assert planner.normalize_plan(plan, 30)["creatives"][1]["format"] == "Carousel"
    assert "no videos" in planner.SYSTEM_PROMPT
    from backend.benchmarks import CATEGORIES
    assert not any("video" in c["guidance"].lower() for c in CATEGORIES.values())


def test_carousel_cards_show_different_views(monkeypatch, plan):
    from backend import creatives, imagegen
    from backend.db import Creative, Plan, PlanVersion, get_session
    prompts = []
    monkeypatch.setattr(imagegen, "generate", lambda prompt, reference=None, aspect="1:1": prompts.append((prompt, aspect)) or b"IMG")
    with get_session() as s:
        p = s.get(Plan, plan)
        content = dict(p.versions[-1].content)
        content["creatives"] = [{**content["creatives"][0], "format": "Carousel"}]
        p.versions.append(PlanVersion(version=len(p.versions) + 1, source="user", content=content))
        s.commit()
        creatives.generate_for_plan(s, p, 0)
        creatives.generate_for_plan(s, p, 0)
        assert "card 1 of a carousel" in prompts[0][0] and "card 2 of a carousel" in prompts[1][0]
        assert prompts[0][1] == "1:1"
        for _ in range(8):
            creatives.generate_for_plan(s, p, 0)
        with pytest.raises(ValueError):
            creatives.generate_for_plan(s, p, 0)  # at most 10 cards
        for c in s.query(Creative).filter(Creative.plan_id == plan).all():
            creatives.delete_file(c.file_name); s.delete(c)
        s.commit()
