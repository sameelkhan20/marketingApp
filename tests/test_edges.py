"""Edge cases across modules: Meta client contract, sync, learning, monitor lifecycle, readiness."""

import json
from datetime import date

import pytest
from sqlalchemy import select

from backend import meta_client as MC
from backend import monitor, outcomes, readiness, settings_store
from backend.db import Launch, LearnedRule, Plan, PlanVersion, get_session
from backend.learning import diff_plans
from backend.meta_sync import aggregate, sync_account
from tests.fakes import FakeMeta


class _Resp:
    def __init__(self, status, data):
        self.status_code, self._data = status, data
        self.content = json.dumps(data).encode()
        self.text = json.dumps(data)

    def json(self):
        return self._data


def test_meta_client_surfaces_meta_error_message(monkeypatch):
    monkeypatch.setattr(MC.requests, "get", lambda *a, **k: _Resp(400, {"error": {"code": 190, "message": "Invalid OAuth"}}))
    with pytest.raises(MC.MetaError, match="190.*Invalid OAuth"):
        MC.MetaClient("t").me()


def test_meta_client_follows_paging(monkeypatch):
    pages = {"first": {"data": [{"id": 1}], "paging": {"next": "https://graph/next"}},
             "https://graph/next": {"data": [{"id": 2}]}}
    monkeypatch.setattr(MC.requests, "get",
                        lambda url, params=None, headers=None, timeout=None:
                        _Resp(200, pages[url if url in pages else "first"]))
    assert [r["id"] for r in MC.MetaClient("t").get_all("x")] == [1, 2]


def test_meta_client_requires_token():
    with pytest.raises(MC.MetaError):
        MC.MetaClient("")


def test_aggregate_handles_missing_fields_and_no_purchases():
    stats = aggregate([{"country": "US", "date_start": "2026-10-01", "spend": "10", "impressions": "1000"}], "USD",
                      today=date(2026, 11, 23))
    assert stats["US"]["clicks"] == 0 and stats["US"]["cvr"] == 0 and stats["US"]["confidence"] == "low"
    assert aggregate([], "USD") == {}


def test_learning_diff():
    assert diff_plans({"a": 1, "b": 2}, {"a": 1, "b": 3, "c": 4}) == ["changed 'b': 2 -> 3", "added 'c'"]
    assert diff_plans({"a": 1}, {"a": 1}) == []


def test_readiness_flags_tiny_budget(plan):
    with get_session() as s:
        p = s.get(Plan, plan)
        p.budget_pkr = 3000  # ~$0.36/day across 3 ad sets
        s.commit()
        r = readiness.check_plan(s, p, live=False)
        budget = next(c for c in r["checks"] if c["key"] == "budget")
        assert not budget["ok"] and budget["hard"] and r["ready"] is False


def test_readiness_meta_settings_missing(plan):
    with get_session() as s:
        r = readiness.check_plan(s, s.get(Plan, plan), live=False)
        failed = {c["key"] for c in r["checks"] if not c["ok"] and c["hard"]}
        assert {"token", "account", "page", "store_url"} <= failed


def test_estimate_api_validation(client, auth):
    base = {"budget_pkr": 100000, "country": "US", "product": "x jacket", "niche": "leather_jackets",
            "price_usd": 120, "duration_days": 30}
    assert client.post("/api/estimate", json={**base, "budget_pkr": 0}, headers=auth).status_code == 422
    assert client.post("/api/estimate", json={**base, "duration_days": 1}, headers=auth).status_code == 422
    assert client.post("/api/estimate", json={**base, "country": "ZZ"}, headers=auth).status_code == 400
    assert client.post("/api/estimate", json={**base, "country": "us"}, headers=auth).status_code == 200


def test_apply_rejects_non_executable_option(client, auth, plan):
    from backend.db import Alert
    with get_session() as s:
        l = Launch(plan_id=plan, campaign_id="APPLY", objects={"ads": [], "adsets": []})
        s.add(l)
        s.commit()
        a = Alert(launch_id=l.id, plan_id=plan, fingerprint="f", severity="warning", kind="low_ctr", object_level="ad",
                  object_id="AD1", object_name="x", message="m", metrics={},
                  suggestions=[{"action": "Change the hook", "expected_effect": "e", "risk": "r"}])
        s.add(a)
        s.commit()
        aid = a.id
    assert client.post(f"/api/alerts/{aid}/apply", json={"option_index": 0}, headers=auth).status_code == 400
    assert client.post(f"/api/alerts/{aid}/apply", json={"option_index": 9}, headers=auth).status_code == 400


def test_automation_bounds(client, auth):
    assert client.post("/api/automation", json={"enabled": {}, "max_budget_pct": 90}, headers=auth).status_code == 422
    r = client.post("/api/automation", json={"enabled": {"bogus": True, "no_purchases": False}, "max_budget_pct": 15}, headers=auth)
    assert r.status_code == 200 and "bogus" not in r.json()["enabled"]


def test_upload_rejects_huge_file(client, auth, plan):
    big = b"\x89PNG" + b"0" * (16 * 1024 * 1024)
    r = client.post(f"/api/plans/{plan}/creatives/upload?creative_index=0", files={"file": ("a.png", big, "image/png")},
                    headers=auth)
    assert r.status_code in (400, 413), "creative upload accepts files of any size"


# ---------- regressions from the code review ----------

def test_ad_actions_do_not_request_daily_budget(monkeypatch):
    asked = {}
    monkeypatch.setattr(MC.MetaClient, "get", lambda self, path, **p: asked.setdefault(path, p["fields"]) and {})
    c = MC.MetaClient("t")
    c.get_object("AD1", "ad")
    c.get_object("A1", "adset")
    assert "daily_budget" not in asked["AD1"] and "daily_budget" in asked["A1"]


def test_meta_errors_never_contain_the_token(monkeypatch):
    def boom(url, params=None, headers=None, timeout=None, data=None):
        assert "access_token" not in (params or {}) and "access_token" not in (data or {})
        raise MC.requests.ConnectionError(f"{url}?access_token=EAAsecretsecretsecretsecret123 failed")
    monkeypatch.setattr(MC.requests, "get", boom)
    with pytest.raises(MC.MetaError) as ex:
        MC.MetaClient("EAAsecretsecretsecretsecret123").me()
    assert "EAAsecret" not in str(ex.value)


def test_non_json_meta_response_is_a_meta_error(monkeypatch):
    class Html:
        status_code, content, text = 502, b"<html>bad gateway</html>", "<html>bad gateway</html>"
        def json(self): raise ValueError("no json")
    monkeypatch.setattr(MC.requests, "get", lambda *a, **k: Html())
    with pytest.raises(MC.MetaError, match="non-JSON"):
        MC.MetaClient("t").me()


def test_temporary_pause_does_not_finalise(plan):
    with get_session() as s:
        l = Launch(plan_id=plan, campaign_id="PAUSE", objects={"ads": [], "adsets": [], "currency": "USD"})
        s.add(l)
        s.commit()
        po = outcomes.update_plan_outcome(s, l, s.get(Plan, plan), FakeMeta().campaign_lifetime("x"), "USD", 10, "PAUSED")
        assert not po.completed


def test_calibration_weights_samples_equally():
    from backend.db import Calibration
    with get_session() as s:
        s.query(Calibration).filter(Calibration.country == "CA").delete()
        s.commit()
        for r in (2.0, 1.0, 1.0):
            outcomes._update_calibration(s, "CA", r)
        s.commit()
        assert s.get(Calibration, "CA").factor == 1.25  # mean of prior 1.0 and 2.0, 1.0, 1.0


def test_approve_only_from_draft_and_edit_resets_to_draft(client, auth):
    body = {"budget_pkr": 150000, "country": "GB", "product": "Parka", "niche": "leather_jackets",
            "price_usd": 140, "duration_days": 21}
    p = client.post("/api/plans", json=body, headers=auth).json()
    assert client.post(f"/api/plans/{p['id']}/approve", headers=auth).status_code == 200
    assert client.post(f"/api/plans/{p['id']}/approve", headers=auth).status_code == 400
    r = client.post(f"/api/plans/{p['id']}/edit", json={"content": p["versions"][0]["content"], "note": ""}, headers=auth)
    assert r.json()["status"] == "draft"


def test_edit_rejects_broken_plan(client, auth, plan):
    c = {"summary": "x"}
    assert client.post(f"/api/plans/{plan}/edit", json={"content": c, "note": ""}, headers=auth).status_code == 422


def test_paused_adset_not_judged():
    from backend.rules import evaluate
    m = [{"level": "adset", "object_id": "A1", "name": "Broad", "effective_status": "PAUSED", "spend_usd": 200,
          "impressions": 9000, "clicks": 40, "purchases": 0, "revenue_usd": 0, "frequency": 4, "ctr": 0.004,
          "cpa_usd": 0, "roas": 0}]
    assert evaluate(m, {"max_cpa_usd": 60, "min_ctr": 0.01, "max_frequency": 3, "min_roas": 1}, 10, 5, "ACTIVE", []) == []


# ---------- behaviour that must hold ----------

def test_editing_twice_does_not_duplicate_rules(client, auth):
    body = {"budget_pkr": 150000, "country": "GB", "product": "Bomber", "niche": "leather_jackets",
            "price_usd": 140, "duration_days": 21}
    p = client.post("/api/plans", json=body, headers=auth).json()
    c = dict(p["versions"][0]["content"])
    c["ad_sets"] = c["ad_sets"][:1]
    client.post(f"/api/plans/{p['id']}/edit", json={"content": c, "note": "Only broad audiences please"}, headers=auth)
    c2 = dict(c)
    c2["summary"] = c["summary"] + " (tweak)"
    r2 = client.post(f"/api/plans/{p['id']}/edit", json={"content": c2, "note": ""}, headers=auth).json()
    assert not any("ad_sets" in r for r in r2["learned_rules"]), \
        "second edit re-learned the first edit's change (diff is against v1 instead of the previous version)"
    rules = [r["rule"] for r in client.get("/api/rules", headers=auth).json()]
    assert rules.count("Only broad audiences please") == 1


def test_completed_launch_is_no_longer_monitored(plan):
    with get_session() as s:
        l = Launch(plan_id=plan, campaign_id="DONE", objects={"ads": [], "adsets": [], "currency": "USD"})
        s.add(l)
        s.commit()
        p = s.get(Plan, plan)
        po = outcomes.update_plan_outcome(s, l, p, FakeMeta().campaign_lifetime("x"), "USD", 30, "ACTIVE")
        s.commit()
        assert po.completed
        active = s.scalars(select(Launch).where(Launch.id == l.id, Launch.status.not_in(monitor.INACTIVE_LAUNCH))).all()
        assert active == [], "completed launch still selected by the monitor"


def test_days_active_counts_from_first_delivery_not_creation(plan):
    """A campaign created paused and activated later must not be judged on the paused days."""
    with get_session() as s:
        l = Launch(plan_id=plan, campaign_id="LATE", objects={"ads": [], "adsets": [], "currency": "USD"})
        s.add(l)
        s.commit()
        fake = FakeMeta()   # created 5 days ago
        fake.campaign_lifetime = lambda cid: {"spend": "20", "date_start": date.today().isoformat(),
                                              "actions": [], "action_values": []}
        monitor.check_launch(s, fake, l, "USD")
        from backend.db import PlanOutcome
        assert s.get(PlanOutcome, l.id).days_active <= 1


def test_sync_rejects_unsupported_currency(monkeypatch):
    class EurClient(FakeMeta):
        def account(self, act):
            return {"currency": "EUR"}
    monkeypatch.setattr("backend.meta_sync.MetaClient", EurClient)
    with get_session() as s, pytest.raises(MC.MetaError, match="EUR"):
        sync_account(s, "t", "1")


def test_rule_reweight_only_touches_rules_used_by_that_plan(plan):
    """A launch's result should only move the weights of rules that existed when its plan was made."""
    from datetime import datetime, timedelta
    with get_session() as s:
        p = s.get(Plan, plan)
        later = LearnedRule(rule="added after this plan", weight=1.0, created_at=p.created_at + timedelta(days=1))
        s.add(later)
        s.commit()
        l = Launch(plan_id=plan, campaign_id="RW", objects={"ads": [], "adsets": [], "currency": "USD"})
        s.add(l)
        s.commit()
        outcomes.update_plan_outcome(s, l, p, FakeMeta().campaign_lifetime("x"), "USD", 30, "ACTIVE")
        s.commit()
        s.refresh(later)
        assert later.weight == 1.0
