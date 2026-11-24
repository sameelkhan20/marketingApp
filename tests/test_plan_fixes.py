"""Small budgets still launch, delivery time moves the cutoffs, and rules stay with their store."""

from datetime import date

from backend import outcomes, seasonality
from backend.budget_fit import fit_ad_sets
from backend.db import LearnedRule, get_session
from backend.learning import rule_fits
from backend.planner import fit_to_budget


def test_small_ad_sets_fold_into_the_largest():
    sets = [{"name": "Broad", "daily_budget_pct": 60}, {"name": "Interests", "daily_budget_pct": 30},
            {"name": "Retargeting", "daily_budget_pct": 10}]
    out, folded = fit_ad_sets(sets, 20, 5)  # retargeting gets $2, interests $6
    assert folded == ["Retargeting"] and [a["name"] for a in out] == ["Broad", "Interests"]
    assert abs(sum(a["daily_budget_pct"] for a in out) - 100) < 0.01 and out[0]["daily_budget_pct"] == 70
    out, folded = fit_ad_sets(sets, 6, 5)
    assert len(out) == 1 and out[0]["daily_budget_pct"] == 100
    out, folded = fit_ad_sets(sets, 1000, 5)
    assert folded == [] and len(out) == 3


def test_plan_is_fitted_and_says_why():
    plan = {"ad_sets": [{"name": "A", "daily_budget_pct": 90}, {"name": "B", "daily_budget_pct": 10}], "risks": []}
    out = fit_to_budget(plan, {"country": "US", "duration_days": 10}, {"daily_usd": 20})
    assert len(out["ad_sets"]) == 1 and "B" in out["risks"][0]


def test_delivery_days_move_the_christmas_cutoff():
    slow = seasonality.events("US", date(2026, 11, 20), date(2026, 12, 24), "leather_jackets", 14)
    fast = seasonality.events("US", date(2026, 11, 20), date(2026, 12, 24), "leather_jackets", 3)
    cut = lambda evs: [e for e in evs if "last order day" in e["name"].lower()]
    assert cut(slow) and cut(fast) and cut(slow)[0]["date"] < cut(fast)[0]["date"]
    assert seasonality.shipping_days("PK") == 3 and seasonality.shipping_days("US") == 14
    assert seasonality.shipping_days("US", 5) == 5


def test_rules_stay_with_their_store():
    r = LearnedRule(rule="Use bold hooks", niche="", brand_id=2, active=1, weight=1)
    assert rule_fits(r, "US", "leather_jackets", 2) and not rule_fits(r, "US", "leather_jackets", 0)
    assert rule_fits(LearnedRule(rule="x", niche="", brand_id=None), "US", "", 5)


def test_result_rules_always_get_the_standard_tag(monkeypatch):
    from backend import llm

    from types import SimpleNamespace as NS
    P = lambda: NS(country="US", niche="leather_jackets", versions=[NS(content={})])
    po = NS(predicted_low=1, predicted_high=2, predicted_expected=1, duration_days=7, purchases=1, days_active=7,
            spend_usd=10, verdict="met", ratio=1.0)
    monkeypatch.setattr(llm, "structured", lambda *a, **k: {"rules": ["[result UK x] Keep carousels", "Raise budget"]})
    out = outcomes._ai_rules(P(), po, {})
    assert out == ["[result US leather_jackets] Keep carousels", "[result US leather_jackets] Raise budget"]


def test_old_niches_apply_to_every_plan():
    from backend.db import init_db
    with get_session() as s:
        s.add(LearnedRule(rule="legacy rule zz", niche="fashion", active=1, weight=1)); s.commit()
    init_db()
    with get_session() as s:
        r = s.query(LearnedRule).filter(LearnedRule.rule == "legacy rule zz").one()
        assert r.niche == ""
        s.delete(r); s.commit()


def test_email_case_does_not_matter(client, auth):
    r = client.post("/api/users/register", json={"email": "Mixed.Case@Example.com", "password": "StaffPass9"}, headers=auth)
    assert r.status_code == 201 and r.json()["email"] == "mixed.case@example.com"
    assert client.post("/api/users/register", json={"email": "mixed.case@example.com", "password": "StaffPass9"},
                       headers=auth).status_code == 400
    ok = client.post("/api/auth/login", json={"email": "MIXED.case@example.com", "password": "StaffPass9"})
    assert ok.status_code == 200


def test_deleted_store_does_not_break_the_images_panel(client, auth):
    r = client.get("/api/creatives/status?brand_id=987", headers=auth)
    assert r.status_code == 200 and r.json()["product_photo"] == ""
