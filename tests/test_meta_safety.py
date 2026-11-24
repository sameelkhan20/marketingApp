"""Keeping the ad accounts safe: Meta policy in the copy, rate limits, and restricted accounts."""

import json
import time

import pytest

from backend import adpolicy, meta_client, monitor, readiness, settings_store
from backend.db import Launch, Plan, PlanVersion, get_session
from backend.meta_client import MetaClient, MetaError, MetaRateLimited
from tests.fakes import FakeMeta


@pytest.fixture(autouse=True)
def _no_pause():
    meta_client._pause.update(until=0.0, why="")
    yield
    meta_client._pause.update(until=0.0, why="")


# ---------- policy ----------

def test_policy_blocks_real_risks_only():
    bad = adpolicy.check_text("First copy Gucci bag, are you overweight?", "x")
    assert {f["level"] for f in bad} == {"block"} and len(bad) == 3
    for ok in ("All black leather jacket", "Coach jacket", "Faux leather", "Off-white hoodie",
               "Are you ready for Black Friday?", "Single-breasted coat", "Mirror polish cutlery"):
        assert adpolicy.check_text(ok, "x") == [], ok
    assert adpolicy.check_text("Free shipping this week", "x", {"offer": ""})[0]["level"] == "warn"
    assert adpolicy.check_text("Free shipping this week", "x", {"offer": "free shipping"}) == []


def test_policy_blocks_launch_and_shows_on_the_plan(client, auth, plan):
    with get_session() as s:
        p = s.get(Plan, plan)
        content = json.loads(json.dumps(p.versions[-1].content))
        content["creatives"][0]["hook"] = "AAA quality first copy of the Schott jacket"
        p.versions.append(PlanVersion(version=len(p.versions) + 1, source="user", content=content))
        p.status = "approved"
        s.commit()
        checks = readiness.check_plan(s, p, live=False)
    assert not checks["ready"] and any(c["key"] == "policy" and c["hard"] for c in checks["checks"])
    out = client.get(f"/api/plans/{plan}", headers=auth).json()
    assert any(f["level"] == "block" for f in out["policy"])
    assert client.post(f"/api/plans/{plan}/launch", headers=auth).status_code == 400


# ---------- rate limits ----------

class _R:
    def __init__(self, status, body, headers=None):
        self.status_code, self._b, self.headers = status, body, headers or {}
        self.content, self.text = b"x", json.dumps(body)

    def json(self):
        return self._b


def test_throttle_pauses_every_meta_call(monkeypatch):
    calls = []

    def fake(url, **kw):
        calls.append(url)
        return _R(400, {"error": {"code": 17, "message": "User request limit reached"}})
    monkeypatch.setattr(meta_client.requests, "get", fake)
    with pytest.raises(MetaRateLimited):
        MetaClient("EAAx").get("act_1")
    with pytest.raises(MetaRateLimited):  # paused: no second request goes out
        MetaClient("EAAx").get("act_1")
    assert len(calls) == 1 and meta_client.paused()[0]


def test_usage_header_slows_down_before_the_limit(monkeypatch):
    usage = {"act_1": [{"call_count": 91, "total_cputime": 10, "total_time": 10, "estimated_time_to_regain_access": 0}]}
    monkeypatch.setattr(meta_client.requests, "get",
                        lambda url, **kw: _R(200, {"id": "act_1"}, {"x-business-use-case-usage": json.dumps(usage)}))
    assert MetaClient("EAAx").get("act_1") == {"id": "act_1"}
    assert meta_client.paused()[0]
    meta_client._pause.update(until=0.0)
    usage["act_1"][0].update(call_count=20, estimated_time_to_regain_access=12)
    MetaClient("EAAx").get("act_1")
    assert meta_client._pause["until"] - time.time() > 11 * 60  # waits as long as Meta says


# ---------- restricted accounts ----------

def test_restricted_account_is_never_changed():
    assert adpolicy.account_problem({"account_status": 1}) == ""
    assert "risk review" in adpolicy.account_problem({"account_status": 7})
    import backend.launcher as L

    class Disabled(FakeMeta):
        def account(self, act):
            return {"currency": "USD", "account_status": 2}
    old = L.MetaClient
    L.MetaClient = Disabled
    try:
        with pytest.raises(MetaError, match="disabled"):
            L.launch_plan({"budget_pkr": 100000, "country": "US", "product": "J", "niche": "leather_jackets",
                           "price_usd": 120, "duration_days": 10}, {"ad_sets": [{"name": "A", "daily_budget_pct": 100}],
                                                                     "creatives": []},
                          {"meta_access_token": "t", "meta_ad_account_id": "1"}, 1)
    finally:
        L.MetaClient = old


def test_monitor_reads_but_does_not_automate_a_restricted_account(plan, monkeypatch):
    from backend import actions
    with get_session() as s:
        for l in s.query(Launch).all():
            l.status = "completed"
        settings_store.set_many(s, {"meta_access_token": "EAAmain", "meta_ad_account_id": "act_1",
                                    "auto_actions": json.dumps({"no_purchases": True})})
        s.add(Launch(plan_id=plan, campaign_id="FAKE_R", objects={"currency": "USD", "adsets": [], "ads": []}, status="active"))
        s.commit()

    class Restricted(FakeMeta):
        def account(self, act):
            return {"currency": "USD", "account_status": 3}
    monkeypatch.setattr(monitor, "MetaClient", Restricted)
    called = []
    monkeypatch.setattr(monitor, "auto_apply", lambda *a, **k: called.append(1) or [])
    r = monitor.run_once()
    assert any(c["campaign_id"] == "FAKE_R" for c in r["checked"])          # still reads the numbers
    assert not called and any("unsettled" in e for e in r["errors"])          # but changes nothing
    with get_session() as s:
        for l in s.query(Launch).filter(Launch.campaign_id == "FAKE_R").all():
            l.status = "completed"
        settings_store.set_many(s, {"meta_access_token": "", "meta_ad_account_id": "", "auto_actions": ""})
    assert actions.AUTOMATABLE_KINDS


def test_restricted_account_gets_no_uploads(client, auth, plan, monkeypatch):
    from backend import main
    calls = []

    class Disabled:
        def __init__(self, token="x"):
            pass

        def account(self, act):
            return {"currency": "USD", "account_status": 7}

        def upload_image_bytes(self, *a, **k):
            calls.append("upload")
            return "H"
    with get_session() as s:
        settings_store.set_many(s, {"meta_access_token": "EAAx", "meta_ad_account_id": "act_1"})
        s.get(Plan, plan).status = "approved"; s.commit()
    monkeypatch.setattr(main, "MetaClient", Disabled)
    monkeypatch.setattr(main.readiness, "check_plan", lambda s, p, live=False: {"ready": True, "checks": []})
    r = client.post(f"/api/plans/{plan}/launch", headers=auth)
    assert r.status_code == 400 and "risk review" in r.json()["detail"] and calls == []
    with get_session() as s:
        assert s.get(Plan, plan).status == "approved"
        settings_store.set_many(s, {"meta_access_token": "", "meta_ad_account_id": ""})


def test_abuse_block_pauses_for_hours_and_cleanup_still_runs(monkeypatch):
    def fake_get(url, **kw):
        return _R(400, {"error": {"code": 368, "message": "deemed abusive"}})
    monkeypatch.setattr(meta_client.requests, "get", fake_get)
    with pytest.raises(MetaRateLimited, match="6 hours"):
        MetaClient("EAAx").get("act_1")
    assert meta_client._pause["until"] - time.time() > 5 * 3600
    sent = []
    monkeypatch.setattr(meta_client.requests, "post", lambda url, **kw: sent.append(url) or _R(200, {"success": True}))
    assert MetaClient("EAAx").delete_now("C1") == {"success": True} and len(sent) == 1
    with pytest.raises(MetaRateLimited):
        MetaClient("EAAx").update_object("C1", status="PAUSED")  # ordinary writes still wait


def test_pause_survives_a_restart():
    meta_client._pause_for(1800, "test pause")
    saved_until = meta_client._pause["until"]
    meta_client._pause.clear()
    meta_client._pause.update(until=0.0, why="")  # a fresh process
    assert meta_client.paused()[0] and abs(meta_client._pause["until"] - saved_until) < 1
    meta_client._pause.update(until=0.0, why="", loaded=True)
    meta_client._save_pause()


def test_rate_limit_never_counts_as_a_failed_auto_try(plan):
    import backend.actions as actions
    from backend.db import ActionLog, Alert
    from tests.test_monitor_actions import _launch
    with get_session() as s:
        s.query(ActionLog).delete(); s.query(Alert).update({"status": "resolved"}); s.commit()
        settings_store.set_many(s, {"auto_actions": json.dumps({"no_purchases": True})})
        l = _launch(s, plan)
        a = Alert(launch_id=l.id, plan_id=plan, fingerprint="rl", severity="warning", kind="no_purchases",
                  object_level="adset", object_id="A1", object_name="A1", message="m", metrics={},
                  suggestions=[{"action": "x", "spec": {"type": "pause", "level": "adset", "object_id": "A1"}}])
        s.add(a); s.commit()

        class Throttled(FakeMeta):
            def get_object(self, oid, level="adset"):
                raise MetaRateLimited("slow down")
        assert actions.auto_apply(s, Throttled(), [a]) == []
        assert s.query(ActionLog).count() == 0 and a.id in {x.id for x in actions.pending_alerts(s)}
        settings_store.set_many(s, {"auto_actions": ""})
