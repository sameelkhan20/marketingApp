"""Profit, season, tracking health, budget rebalancing and the Today page."""

from datetime import date, datetime, timedelta, timezone

import pytest

from backend import actions, seasonality, today, tracking
from backend.db import Launch, Plan, get_session
from backend.estimator import estimate_orders
from backend.monitor import _with_target, with_specs
from backend.rules import TEMPLATE_SUGGESTIONS, evaluate_rebalance
from tests.fakes import FakeMeta


# ---------- profit ----------

def test_profit_and_break_even():
    e = estimate_orders(280000, "US", "leather_jackets", 120, 30, unit_cost_usd=35, shipping_usd=15,
                        start=date(2026, 6, 1))
    assert e.margin_usd == 70 and e.break_even_cpa_usd == 70
    # from the unrounded order count, so rounding never flips a loss into a profit
    assert e.profit_usd == pytest.approx(e.assumptions["orders_raw"] * 70 - e.budget_usd, abs=0.05)
    assert e.profit_low_usd <= e.profit_usd <= e.profit_high_usd


def test_losing_plan_is_flagged():
    e = estimate_orders(280000, "US", "leather_jackets", 60, 30, unit_cost_usd=40, shipping_usd=15,
                        start=date(2026, 6, 1))
    assert e.cpa_usd > e.margin_usd and any("loses" in w for w in e.warnings)
    e2 = estimate_orders(280000, "US", "leather_jackets", 50, 30, unit_cost_usd=45, shipping_usd=10)
    assert any("does not cover" in w for w in e2.warnings)


def test_no_cost_means_no_profit_figures():
    e = estimate_orders(280000, "US", "leather_jackets", 120, 30)
    assert e.profit_usd is None and e.break_even_cpa_usd == 0


# ---------- season ----------

def test_black_friday_and_cutoff_dates_2026():
    ev = {e["name"]: e["date"] for e in seasonality.events("US", date(2026, 11, 1), date(2026, 12, 31))}
    assert ev["Black Friday"] == "2026-11-27" and ev["Cyber Monday"] == "2026-11-30"
    assert ev["Last order day for Christmas delivery"] == "2026-12-10"
    assert "Boxing Day sales" not in ev  # US has no Boxing Day
    assert "Boxing Day sales" in {e["name"] for e in seasonality.events("GB", date(2026, 12, 1), date(2026, 12, 31))}


def test_season_shapes_the_estimate():
    winter = estimate_orders(280000, "US", "leather_jackets", 120, 30, start=date(2026, 11, 1))
    summer = estimate_orders(280000, "US", "leather_jackets", 120, 30, start=date(2026, 6, 1))
    assert winter.season["label"] == "peak season" and summer.season["label"] == "off season"
    assert winter.cvr > summer.cvr and winter.cpm_usd > summer.cpm_usd
    assert any("Off season" in w for w in summer.warnings)
    assert any("Black Friday" in w for w in winter.warnings)
    # southern hemisphere is flipped: June is winter in Australia
    au_june = seasonality.season_factors("AU", date(2026, 6, 1), 30)
    us_june = seasonality.season_factors("US", date(2026, 6, 1), 30)
    assert au_june["cvr"] > us_june["cvr"]


# ---------- tracking ----------

def _ts(hours_ago):
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S+0000")


def test_tracking_problems():
    ok = {"last_fired_time": _ts(1), "counts": {"PageView": 900, "AddToCart": 40, "Purchase": 6}}
    assert tracking.problems(ok, clicks_7d=500) == []
    stale = tracking.problems({"last_fired_time": _ts(72), "counts": {}}, 0)
    assert stale[0]["severity"] == "critical" and "72" in stale[0]["text"]
    no_buy = tracking.problems({"last_fired_time": _ts(1), "counts": {"PageView": 900, "AddToCart": 30}}, clicks_7d=400)
    assert [p["severity"] for p in no_buy] == ["warning"]
    early = tracking.problems({"last_fired_time": _ts(1), "counts": {"PageView": 40}}, clicks_7d=20)
    assert early == []  # too few clicks to judge
    assert tracking.problems({"last_fired_time": None}, 0)[0]["severity"] == "critical"
    assert TEMPLATE_SUGGESTIONS["tracking"]


def test_monitor_raises_tracking_alert(plan):
    from backend import monitor, settings_store
    from backend.db import Alert
    with get_session() as s:
        settings_store.set_many(s, {"meta_pixel_id": "PX1"})
        l = Launch(plan_id=plan, campaign_id="TRK", objects={"ads": [], "adsets": [], "currency": "USD"})
        s.add(l)
        s.commit()
        fake = FakeMeta()
        fake.pixel_health = lambda pid: {"last_fired_time": _ts(80), "counts": {}}
        monitor.check_launch(s, fake, l, "USD")
        a = s.query(Alert).filter(Alert.launch_id == l.id, Alert.kind == "tracking").one()
        assert a.severity == "critical" and a.suggestions
        settings_store.set_many(s, {"meta_pixel_id": ""})


# ---------- rebalance ----------

def _adset(oid, name, spend, purchases):
    return {"level": "adset", "object_id": oid, "name": name, "effective_status": "ACTIVE", "spend_usd": spend,
            "impressions": 10000, "clicks": 100, "purchases": purchases, "revenue_usd": 0, "frequency": 1,
            "ctr": 0.01, "cpa_usd": round(spend / purchases, 2) if purchases else 0, "roas": 0}


def test_rebalance_rule():
    m = [_adset("A1", "Broad", 120, 5), _adset("A2", "Interest", 90, 0), _adset("A3", "LAL", 80, 1)]
    f = evaluate_rebalance(m, 60)
    assert len(f) == 1 and f[0]["object_id"] == "A2" and f[0]["metrics"]["to_id"] == "A1"
    assert evaluate_rebalance([_adset("A1", "Broad", 120, 5), _adset("A3", "LAL", 50, 1)], 60) == []
    opts = _with_target(with_specs(TEMPLATE_SUGGESTIONS["rebalance"], "adset", "A2"), f[0])
    assert opts[0]["spec"] == {"type": "move_budget", "level": "adset", "object_id": "A2", "pct": 20.0, "to": "A1"}


def test_move_budget_keeps_total_and_undoes(plan):
    with get_session() as s:
        l = Launch(plan_id=plan, campaign_id="MB", objects={"ads": [], "adsets": [], "currency": "USD"})
        s.add(l)
        s.commit()
        fake = FakeMeta()
        fake.objects["A1"]["daily_budget"] = "4000"
        fake.objects["A2"]["daily_budget"] = "3000"
        log = actions.execute(s, fake, {"type": "move_budget", "level": "adset", "object_id": "A2", "to": "A1", "pct": 20},
                              "manual", launch_id=l.id)
        assert log.ok, log.error
        assert (fake.objects["A2"]["daily_budget"], fake.objects["A1"]["daily_budget"]) == ("2400", "4600")
        rev = actions.undo(s, fake, log)
        assert rev.ok and (fake.objects["A2"]["daily_budget"], fake.objects["A1"]["daily_budget"]) == ("3000", "4000")
        # never below Meta's minimum (2x $5 = 1000 cents for US): nothing to move from a 1000-cent ad set
        fake.objects["A2"]["daily_budget"] = "1000"
        bad = actions.execute(s, fake, {"type": "move_budget", "level": "adset", "object_id": "A2", "to": "A1", "pct": 20},
                              "manual", launch_id=l.id)
        assert not bad.ok and "minimum" in bad.error
    assert "rebalance" in actions.AUTOMATABLE_KINDS


# ---------- today ----------

def test_today_lists_events_and_waiting_plans(plan):
    with get_session() as s:
        p = s.get(Plan, plan)
        p.status = "draft"
        s.commit()
    t = today.build()
    titles = [i["title"] for i in t["items"]]
    assert any(f"Plan #{plan} waits for approval" == x for x in titles)
    assert {"campaigns", "orders", "spend_usd", "open_alerts"} <= set(t["stats"])
    order = [today.PRIORITY[i["priority"]] for i in t["items"]]
    assert order == sorted(order)


def test_today_api(client, auth):
    r = client.get("/api/today", headers=auth)
    assert r.status_code == 200 and "items" in r.json()
    assert client.get("/api/today").status_code == 401


def test_plan_api_accepts_costs(client, auth):
    body = {"budget_pkr": 280000, "country": "US", "product": "Biker jacket", "niche": "leather_jackets",
            "price_usd": 120, "duration_days": 30, "unit_cost_usd": 35, "shipping_usd": 15}
    e = client.post("/api/estimate", json=body, headers=auth).json()
    assert e["margin_usd"] == 70 and e["season"]["label"]
    p = client.post("/api/plans", json=body, headers=auth).json()
    assert p["inputs"]["unit_cost_usd"] == 35
    t = p["versions"][0]["content"]["monitoring_targets"]
    assert t["max_cpa_usd"] <= 70 * 0.9 + 0.01  # targets follow the margin
