from datetime import date

from backend.db import AccountStat, get_session
from backend.estimator import estimate_orders
from backend.history import account_overrides
from backend.meta_sync import aggregate


def test_estimate_ranges_and_warnings():
    e = estimate_orders(100000, "US", "leather_jackets", 120, 30)
    assert e.orders_low <= e.orders_expected <= e.orders_high
    assert e.source == "benchmark" and e.confidence == "low"
    low_budget = estimate_orders(5000, "US", "leather_jackets", 120, 30)
    assert any("Daily budget" in w for w in low_budget.warnings)


def test_niche_and_calibration_change_estimate():
    base = estimate_orders(200000, "US", "leather_jackets", 120, 30)
    celeb = estimate_orders(200000, "US", "celebrity_jackets", 120, 30)
    assert celeb.ctr > base.ctr
    cal = estimate_orders(200000, "US", "leather_jackets", 120, 30, calibration=(1.4, 1))
    assert cal.orders_expected > base.orders_expected and cal.assumptions["calibration_factor"] == 1.4


def _rows():
    rows = []
    for m in range(24):
        y, mo = divmod(m, 12)
        ds = f"{2025 + y}-{mo + 1:02d}-01"
        rows.append({"country": "US", "date_start": ds, "spend": 400 if ds >= "2026-08-24" else 300,
                     "impressions": 20000, "inline_link_clicks": 260,
                     "actions": [{"action_type": "omni_purchase", "value": "7"}, {"action_type": "purchase", "value": "7"}],
                     "action_values": [{"action_type": "omni_purchase", "value": "980"}]})
    rows.append({"country": "GB", "date_start": "2026-10-01", "spend": 50, "impressions": 5000,
                 "inline_link_clicks": 60, "actions": [{"action_type": "purchase", "value": "1"}]})
    return rows


def test_aggregate_weights_recent_and_dedupes_purchases():
    stats = aggregate(_rows(), "USD", today=date(2026, 11, 23))
    us = stats["US"]
    assert us["purchases"] == 24 * 7          # omni_purchase preferred, purchase not double counted
    assert 15.0 < us["cpm_usd"] < 20.0        # recent months weighted 2x pull CPM above the plain mean (15.6)
    assert us["confidence"] == "high" and stats["GB"]["confidence"] == "low"


def test_account_history_overrides_benchmark():
    stats = aggregate(_rows(), "USD", today=date(2026, 11, 23))
    with get_session() as s:
        s.query(AccountStat).delete()
        for c, st in stats.items():
            s.add(AccountStat(country=c, since="2024-11-23", until="2026-11-23", **st))
        s.commit()
    ov = account_overrides("US", "leather_jackets", 120)
    assert ov["confidence"] == "high" and ov["source"].startswith("account_history")
    assert account_overrides("GB", "leather_jackets") is None   # < 10k impressions
    e = estimate_orders(200000, "US", "leather_jackets", 120, 30, overrides=ov)
    assert e.source.startswith("account_history") and e.confidence == "high"
    with get_session() as s:
        s.query(AccountStat).delete()
        s.commit()
