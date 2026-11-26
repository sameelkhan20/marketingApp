"""Regressions from the review of categories and stores."""

from datetime import date

from backend import brands, learning, outcomes, seasonality, settings_store
from backend.db import AccountStat, Calibration, LearnedRule, Plan, get_session
from backend.history import account_overrides


def test_jacket_season_is_as_before_categories():
    # Pakistan keeps its full winter season for jackets (it was damped by mistake)
    pk = seasonality.season_factors("PK", date(2026, 12, 1), 30, "leather_jackets")
    assert pk["label"] == "peak season" and pk["cvr"] > 1.15
    # jackets get no Mother's Day lift; Valentine's is 1.10 as before
    may = {e["name"] for e in seasonality.events("US", date(2026, 5, 1), date(2026, 5, 31), "leather_jackets")}
    assert "Mother's Day" not in may
    feb = {e["name"]: e for e in seasonality.events("US", date(2026, 2, 1), date(2026, 2, 28), "leather_jackets")}
    assert feb["Valentine's Day"]["cvr"] == 1.10
    # an event that does not fit the category is left out entirely (no misleading note)
    swim = {e["name"] for e in seasonality.events("GB", date(2026, 12, 1), date(2026, 12, 31), "swimwear_summer")}
    assert "Last order day for Christmas delivery" not in swim


def test_multi_day_events_lift_every_day():
    # the whole last 10 days of Ramadan 2027 (from 28 Feb) are inside this campaign
    f = seasonality.season_factors("PK", date(2027, 2, 20), 30, "kitchen_cutlery")
    plain = seasonality.season_factors("PK", date(2027, 6, 1), 30, "kitchen_cutlery")
    assert f["cvr"] > plain["cvr"] + 0.04
    # an event that started before the campaign still counts for its remaining days
    late = seasonality.events("PK", date(2027, 3, 5), date(2027, 3, 20), "apparel")
    assert any(e["name"] == "Last 10 days of Ramadan" for e in late)


def test_history_only_for_the_synced_store_and_its_categories():
    with get_session() as s:
        s.merge(AccountStat(country="US", since="2025-01-01", until="2026-11-01", spend_usd=9000, impressions=900000,
                            clicks=9000, purchases=120, revenue_usd=18000, cpm_usd=10, ctr=0.01, cvr=0.013, confidence="high"))
        s.commit()
    assert account_overrides("US", "leather_jackets", 150, 0) is not None
    assert account_overrides("US", "other", 60, 0) is not None            # the main store's own account, any product
    assert account_overrides("US", "leather_jackets", 150, 3) is None     # nor about another store
    with get_session() as s:
        s.query(AccountStat).filter(AccountStat.country == "US").delete(); s.commit()


def test_calibration_kept_per_store_and_category():
    assert outcomes.cal_key("us", 0, "leather_jackets") == "US"           # the old key carries on for jackets
    assert outcomes.cal_key("PK", 2, "kitchen_cutlery") == "PK|2|kitchen_cutlery"
    with get_session() as s:
        s.query(Calibration).filter(Calibration.country.in_(["ZQ", "ZQ|2|kitchen_cutlery"])).delete(); s.commit()
        outcomes._update_calibration(s, outcomes.cal_key("ZQ", 2, "kitchen_cutlery"), 0.5); s.commit()
        assert outcomes.calibration_factor(s, "ZQ", 2, "kitchen_cutlery")[1] == 1
        assert outcomes.calibration_factor(s, "ZQ", 0, "leather_jackets") == (1.0, 0)


def test_rules_stay_with_their_category():
    with get_session() as s:
        s.add(LearnedRule(rule="Jackets: always show stitching close-ups", niche="leather_jackets", weight=9))
        s.add(LearnedRule(rule="Global: keep testing 3 creatives", niche="", weight=9))
        s.commit()
        cut = learning.active_rules(s, "PK", "kitchen_cutlery")
        assert "Global: keep testing 3 creatives" in cut and "Jackets: always show stitching close-ups" not in cut
        assert "Jackets: always show stitching close-ups" in learning.active_rules(s, "US", "leather_jackets")


def test_store_ids_never_reused_and_photo_per_store(client, auth):
    a = client.post("/api/brands", json={"values": {"name": "Temp A"}}, headers=auth).json()["id"]
    assert client.delete(f"/api/brands/{a}", headers=auth).status_code == 200
    b = client.post("/api/brands", json={"values": {"name": "Temp B"}}, headers=auth).json()["id"]
    assert b != a
    client.post(f"/api/creatives/brand-style?brand_id={b}", json={"brand_style": "bright marble"}, headers=auth)
    assert client.get(f"/api/creatives/status?brand_id={b}", headers=auth).json()["brand_style"] == "bright marble"
    assert client.get("/api/creatives/status", headers=auth).json()["brand_style"] != "bright marble"
    assert client.get("/api/creatives/status?brand_id=9999", headers=auth).json()["brand_style"] == ""  # deleted store: empty, not an error
    client.delete(f"/api/brands/{b}", headers=auth)


def test_store_delete_rules(client, auth, plan):
    bid = client.post("/api/brands", json={"values": {"name": "Temp C"}}, headers=auth).json()["id"]
    with get_session() as s:
        p = s.get(Plan, plan)
        p.brand_id, p.status = bid, "draft"
        s.commit()
    r = client.delete(f"/api/brands/{bid}", headers=auth)
    assert r.status_code == 400 and "not launched" in r.json()["detail"]
    with get_session() as s:
        s.get(Plan, plan).status = "launched"; s.commit()   # launched, no running campaign left
    assert client.delete(f"/api/brands/{bid}", headers=auth).status_code == 200
    with get_session() as s:
        s.get(Plan, plan).brand_id = 0; s.commit()


def test_today_checks_other_stores(client, auth):
    bid = client.post("/api/brands", json={"values": {"name": "Unconnected Store"}}, headers=auth).json()["id"]
    body = {"budget_pkr": 100000, "country": "PK", "product": "Cutlery set", "niche": "kitchen_cutlery", "brand_id": bid}
    pid = client.post("/api/plans", json=body, headers=auth).json()["id"]
    titles = [i["title"] for i in client.get("/api/today", headers=auth).json()["items"]]
    assert "Connect Unconnected Store to Meta" in titles
    client.delete(f"/api/plans/{pid}", headers=auth)
    client.delete(f"/api/brands/{bid}", headers=auth)


def test_format_words_and_today_label(monkeypatch):
    from backend import planner, today
    from backend.estimator import estimate_orders, estimate_to_dict
    inputs = {"budget_pkr": 100000, "country": "US", "product": "Wallet", "niche": "bags_accessories", "price_usd": 40, "duration_days": 30}
    plan = planner._template_plan(inputs, estimate_to_dict(estimate_orders(100000, "US", "bags_accessories", 40)), [])
    plan["creatives"][1]["format"] = "Static image 1:1 (gift box)"
    plan["creatives"][2]["format"] = "Static image 9:16 for Reels"
    out = planner.normalize_plan(plan, 30)
    assert [c["format"] for c in out["creatives"]][1:] == ["Static image 1:1 (gift box)", "Static image 9:16 for Reels"]
    plan["creatives"][1]["format"] = "Short video (Reels)"
    assert planner.normalize_plan(plan, 30)["creatives"][1]["format"] == "Carousel"
    # during Ramadan the Today page says "now", never "in -3 days"
    import datetime as dt

    class FakeDate(dt.date):
        @classmethod
        def today(cls):
            return dt.date(2027, 3, 3)
    monkeypatch.setattr(today, "date", FakeDate)
    titles = [i["title"] for i in today.build()["items"]]
    assert not any("in -" in t for t in titles)
    assert any(t.startswith("Last 10 days of Ramadan now") for t in titles)

