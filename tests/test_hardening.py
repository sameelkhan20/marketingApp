"""Regression tests for the second review: money safety, AI output, estimates, learning and security."""

import json
from datetime import date, datetime, timedelta

import pytest

from backend import actions, learning, outcomes, planner, settings_store
from backend.db import ActionLog, Alert, Calibration, Launch, LearnedRule, Plan, Setting, get_session
from backend.estimator import estimate_orders, estimate_to_dict
from tests.fakes import FakeMeta
from tests.test_monitor_actions import _launch


# ---------- automation guardrails ----------

def _alert(s, l, plan, kind, oid, specs):
    a = Alert(launch_id=l.id, plan_id=plan, fingerprint=f"{kind}:{oid}:{datetime.utcnow().timestamp()}", severity="warning",
              kind=kind, object_level="adset", object_id=oid, object_name=oid, message="m", metrics={},
              suggestions=[{"action": "x", "expected_effect": "", "risk": "", "spec": sp} for sp in specs])
    s.add(a)
    s.commit()
    return a


def test_auto_one_change_per_object_and_kind_whitelist(plan):
    with get_session() as s:
        s.query(ActionLog).delete(); s.commit()
        l = _launch(s, plan)
        fake = FakeMeta()
        fake.objects["A1"]["daily_budget"] = "4000"
        settings_store.set_many(s, {"auto_max_budget_pct": "20", "auto_actions": json.dumps({"high_cpa": True, "low_roas": True,
                                                                "scale_candidate": True, "creative_winner": True})})
        cut1 = _alert(s, l, plan, "high_cpa", "A1", [{"type": "budget_pct", "level": "adset", "object_id": "A1", "pct": -20}])
        cut2 = _alert(s, l, plan, "low_roas", "A1", [{"type": "budget_pct", "level": "adset", "object_id": "A1", "pct": -20}])
        # the AI put a cut first for a scale candidate: automation must not run it
        wrong = _alert(s, l, plan, "scale_candidate", "A2", [{"type": "budget_pct", "level": "adset", "object_id": "A2", "pct": -20}])
        logs = actions.auto_apply(s, fake, [cut1, cut2, wrong])
        assert len(logs) == 1 and logs[0].ok and fake.objects["A1"]["daily_budget"] == "3200"  # one 20% cut, not 36%
        assert fake.objects["A2"]["daily_budget"] == "1000"
        settings_store.set_many(s, {"auto_actions": ""})


def test_auto_waits_while_tracking_is_broken(plan):
    with get_session() as s:
        s.query(ActionLog).delete(); s.commit()
        l = _launch(s, plan)
        fake = FakeMeta()
        settings_store.set_many(s, {"auto_max_budget_pct": "20",
                                    "auto_actions": json.dumps({"no_purchases": True, "scale_candidate": True})})
        s.add(Alert(launch_id=l.id, plan_id=plan, fingerprint="trk", severity="critical", kind="tracking",
                    object_level="campaign", object_id="C#pixel-stale", object_name="Pixel", message="pixel down",
                    metrics={}, suggestions=[]))
        s.commit()
        # adding budget on purchase data that cannot be trusted: refused
        fake.objects["A1"]["status"] = "PAUSED"  # leave A2 headroom under the plan's daily total
        fake.objects["A2"]["daily_budget"] = "500"
        up = _alert(s, l, plan, "scale_candidate", "A2", [{"type": "budget_pct", "level": "adset", "object_id": "A2", "pct": 20}])
        assert actions.auto_apply(s, fake, [up]) == [] and fake.objects["A2"]["daily_budget"] == "500"
        # pausing only reduces spend: still allowed
        fake.objects["A1"]["status"] = "ACTIVE"
        a = _alert(s, l, plan, "no_purchases", "A1", [{"type": "pause", "level": "adset", "object_id": "A1"}])
        assert len(actions.auto_apply(s, fake, [a])) == 1 and fake.objects["A1"]["status"] == "PAUSED"
        # an info-level tracking note (AddToCart missing) does not block anything
        s.query(Alert).filter(Alert.fingerprint == "trk").update({"severity": "info"}); s.commit()
        assert len(actions.auto_apply(s, fake, [up])) == 1 and fake.objects["A2"]["daily_budget"] == "600"
        settings_store.set_many(s, {"auto_actions": ""})


def test_skipped_alerts_are_retried_later(plan):
    with get_session() as s:
        s.query(ActionLog).delete()
        s.query(Alert).update({"status": "resolved"}); s.commit()
        l = _launch(s, plan)
        settings_store.set_many(s, {"auto_actions": json.dumps({"no_purchases": True})})
        a = _alert(s, l, plan, "no_purchases", "A1", [{"type": "pause", "level": "adset", "object_id": "A1"}])
        assert a.id in {x.id for x in actions.pending_alerts(s)}
        fake = FakeMeta()
        actions.auto_apply(s, fake, actions.pending_alerts(s))
        assert a.id not in {x.id for x in actions.pending_alerts(s)}  # handled once, never again
        settings_store.set_many(s, {"auto_actions": ""})


def test_plan_numbers_edge_cases():
    assert planner._num("1.2%", "min_ctr", percent_as_fraction=True) == pytest.approx(0.012)
    with pytest.raises(planner.PlanError):
        planner._num("1e3abc", "x")
    phases = [{"days": 30}, {"days": 10}]
    planner._fit_days(phases, 30)
    assert sum(p["days"] for p in phases) == 30 and all(p["days"] >= 1 for p in phases)
    phases = [{"days": 1}, {"days": 1}, {"days": 1}]
    planner._fit_days(phases, 2)
    assert sum(p["days"] for p in phases) == 2


def test_zero_sale_launch_lowers_calibration():
    with get_session() as s:
        s.query(Calibration).filter(Calibration.country == "ZY").delete(); s.commit()
        outcomes._update_calibration(s, "ZY", 0.0)
        s.commit()
        c = s.get(Calibration, "ZY")
        assert c.samples == 1 and c.factor < 1.0


def test_old_raw_blacklist_rows_are_hashed():
    import hashlib

    from sqlalchemy import text

    from backend.auth.dependencies import is_blacklisted
    from backend.db import _add_missing_columns, engine
    raw = "x" * 120
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO token_blacklist (token, expires_at) VALUES (:t, :e)"), {"t": raw, "e": 9999999999})
    _add_missing_columns()
    assert is_blacklisted(raw)
    with engine.begin() as conn:
        assert conn.execute(text("SELECT count(*) FROM token_blacklist WHERE token = :t"), {"t": raw}).scalar() == 0
        conn.execute(text("DELETE FROM token_blacklist WHERE token = :h"), {"h": hashlib.sha256(raw.encode()).hexdigest()})


def test_move_budget_rollback_failure_is_reported(plan):
    with get_session() as s:
        l = _launch(s, plan)
        fake = FakeMeta()
        fake.objects["A1"]["daily_budget"] = "4000"; fake.objects["A2"]["daily_budget"] = "3000"
        calls = {"n": 0}
        real = fake.update_object

        def flaky(oid, **f):
            calls["n"] += 1
            if calls["n"] >= 2:
                from backend.meta_client import MetaError
                raise MetaError("rate limited")
            return real(oid, **f)
        fake.update_object = flaky
        log = actions.execute(s, fake, {"type": "move_budget", "level": "adset", "object_id": "A2", "to": "A1", "pct": 20},
                              "manual", launch_id=l.id)
        assert not log.ok and "Ads Manager" in log.error and log.before == {"daily_budget": "3000", "to_budget": "4000"}


# ---------- launch ----------

def test_double_launch_is_refused(client, auth, plan, monkeypatch, active_meta):
    from backend import main
    with get_session() as s:
        settings_store.set_many(s, {"meta_access_token": "EAAtest", "meta_ad_account_id": "act_1", "meta_page_id": "1",
                                    "store_url": "https://x.test", "creative_image_urls": "https://x.test/a.jpg"})
    monkeypatch.setattr(main.readiness, "check_plan", lambda s, p, live=False: {"ready": True, "checks": []})
    calls = []

    def fake_launch(*a, **k):
        calls.append(1)
        # while Meta is being called, a second click arrives
        r = client.post(f"/api/plans/{plan}/launch", headers=auth)
        assert r.status_code in (400, 409)
        return {"campaign_id": "CAMP1", "adsets": [], "ads": [], "notes": [], "currency": "USD"}
    monkeypatch.setattr(main, "launch_plan", fake_launch)
    r = client.post(f"/api/plans/{plan}/launch", headers=auth)
    assert r.status_code == 200 and r.json()["status"] == "launched" and len(calls) == 1
    with get_session() as s:
        settings_store.set_many(s, {"meta_access_token": "", "meta_ad_account_id": "", "meta_page_id": "",
                                    "store_url": "", "creative_image_urls": ""})


def test_failed_launch_releases_the_plan(client, auth, plan, monkeypatch, active_meta):
    from backend import main
    from backend.meta_client import MetaError
    monkeypatch.setattr(main.readiness, "check_plan", lambda s, p, live=False: {"ready": True, "checks": []})
    monkeypatch.setattr(main, "launch_plan", lambda *a, **k: (_ for _ in ()).throw(MetaError("boom")))
    assert client.post(f"/api/plans/{plan}/launch", headers=auth).status_code == 400
    with get_session() as s:
        assert s.get(Plan, plan).status == "approved"


def test_advantage_audience_age_range():
    from backend.launcher import _targeting
    t = _targeting("US", {"name": "Broad", "audience": "US, 30-45, Advantage+"})
    assert t["targeting_automation"]["advantage_audience"] == 1 and t["age_max"] == 65 and t["age_min"] <= 25
    strict = _targeting("US", {"name": "Interests", "audience": "US, 30-45, leather"})
    assert (strict["age_min"], strict["age_max"]) == (30, 45)


# ---------- AI output ----------

def test_normalize_plan_fixes_text_numbers_and_units():
    inputs = {"budget_pkr": 100000, "country": "US", "product": "J", "niche": "leather_jackets", "price_usd": 120, "duration_days": 30}
    c = planner._template_plan(inputs, estimate_to_dict(estimate_orders(100000, "US")), [])
    c["ad_sets"][0]["daily_budget_pct"] = "40%"
    for a in c["ad_sets"]:
        a["daily_budget_pct"] = float(str(a["daily_budget_pct"]).rstrip("%")) / 100  # fractions
    c["monitoring_targets"]["max_cpa_usd"] = "$45"
    c["monitoring_targets"]["min_ctr"] = 1.2  # meant 1.2%
    c["ad_sets"][0]["evil<key>"] = "x"
    out = planner.normalize_plan(c, 30)
    assert sum(a["daily_budget_pct"] for a in out["ad_sets"]) == pytest.approx(100)
    assert out["monitoring_targets"]["max_cpa_usd"] == 45 and out["monitoring_targets"]["min_ctr"] == pytest.approx(0.012)
    assert "evil<key>" not in out["ad_sets"][0]
    assert sum(p["days"] for p in out["phases"]) == 30
    with pytest.raises(planner.PlanError):
        planner.normalize_plan({**c, "ad_sets": []})
    with pytest.raises(planner.PlanError):
        planner.normalize_plan({**c, "monitoring_targets": {**c["monitoring_targets"], "min_roas": "lots"}})


def test_template_never_has_zero_day_phase():
    inputs = {"budget_pkr": 100000, "country": "US", "product": "J", "niche": "leather_jackets", "price_usd": 120, "duration_days": 3}
    c = planner._template_plan(inputs, estimate_to_dict(estimate_orders(100000, "US", duration_days=3)), [])
    assert all(p["days"] >= 1 for p in c["phases"]) and sum(p["days"] for p in c["phases"]) == 3


def test_unknown_niche_rejected(client, auth):
    body = {"budget_pkr": 100000, "country": "US", "product": "Jacket", "niche": "Celebrity_Jackets", "price_usd": 120}
    assert client.post("/api/estimate", json=body, headers=auth).status_code == 200  # case is normalised
    assert client.post("/api/estimate", json={**body, "niche": "shoes"}, headers=auth).status_code == 422


# ---------- estimates and outcomes ----------

def test_profit_sign_follows_real_orders():
    e = estimate_orders(30000, "US", "leather_jackets", 300, 30, unit_cost_usd=150, start=date(2026, 6, 1))
    loses = any("loses" in w for w in e.warnings)
    assert (e.profit_usd < 0) == loses or e.cpa_usd <= e.margin_usd


def test_zero_expected_is_not_behind(plan):
    with get_session() as s:
        p = s.get(Plan, plan)
        p.estimate = {**p.estimate, "orders_expected": 0, "assumptions": {**p.estimate["assumptions"], "orders_raw": 0.2}}
        l = _launch(s, plan)
        po = outcomes.update_plan_outcome(s, l, p, {"spend": "50", "actions": [{"action_type": "purchase", "value": "4"}]},
                                          "USD", 5, "ACTIVE")
        assert po.verdict == "no_data"
        s.rollback()


def test_calibration_converges_to_true_factor():
    """A market that converts at half the raw estimate must settle near 0.5, not near its square root."""
    with get_session() as s:
        s.query(Calibration).filter(Calibration.country == "ZZ").delete(); s.commit()
        factor = 1.0
        for _ in range(30):
            observed = 0.5 / factor  # actual / calibrated prediction
            outcomes._update_calibration(s, "ZZ", observed * factor)  # what complete_launch now passes
            s.commit()
            factor = s.get(Calibration, "ZZ").factor
        assert factor < 0.56


def test_result_rules_only_for_their_market():
    assert learning.applies_to("[result GB leather_jackets] Broad wins", "GB", "leather_jackets")
    assert not learning.applies_to("[result GB leather_jackets] Broad wins", "US", "leather_jackets")
    assert learning.applies_to("Always test 3 creatives", "US", "celebrity_jackets")
    with get_session() as s:
        s.add(LearnedRule(rule="[result GB leather_jackets] only for GB", weight=5))
        s.commit()
        assert "[result GB leather_jackets] only for GB" not in learning.active_rules(s, "US", "leather_jackets")
        assert "[result GB leather_jackets] only for GB" in learning.active_rules(s, "GB", "leather_jackets")


# ---------- security ----------

def test_password_change_signs_out_other_sessions(client, auth):
    client.post("/api/users/register", json={"email": "tv@example.com", "password": "TvPass123"}, headers=auth)
    tok = client.post("/api/auth/login", json={"email": "tv@example.com", "password": "TvPass123"}).json()
    old = {"Authorization": f"Bearer {tok['access_token']}"}
    r = client.post("/api/users/me/password", json={"current_password": "TvPass123", "new_password": "TvPass456"}, headers=old)
    assert r.status_code == 200 and r.json()["access_token"]
    assert client.get("/api/today", headers=old).status_code == 401
    assert client.post("/api/auth/refresh", json={"refresh_token": tok["refresh_token"]}).status_code == 401
    new = {"Authorization": f"Bearer {r.json()['access_token']}"}
    assert client.get("/api/today", headers=new).status_code == 200


def test_refresh_token_is_single_use(client, auth):
    from tests.conftest import OWNER
    tok = client.post("/api/auth/login", json=OWNER).json()
    first = client.post("/api/auth/refresh", json={"refresh_token": tok["refresh_token"]})
    again = client.post("/api/auth/refresh", json={"refresh_token": tok["refresh_token"]})
    assert first.status_code == 200 and again.status_code == 401


def test_login_throttled(client):
    from backend.auth import routes
    routes._fails.clear()
    for _ in range(5):
        assert client.post("/api/auth/login", json={"email": "victim@example.com", "password": "wrongpass"}).status_code == 401
    assert client.post("/api/auth/login", json={"email": "victim@example.com", "password": "wrongpass"}).status_code == 429
    routes._fails.clear()


def test_meta_token_is_stored_encrypted():
    with get_session() as s:
        settings_store.set_many(s, {"meta_access_token": "EAAsecret123"})
        row = s.get(Setting, "meta_access_token")
        assert not row or "EAAsecret123" not in (row.value or "")
        enc = s.get(Setting, "secret:meta_access_token")
        assert enc and "EAAsecret123" not in enc.value
        assert settings_store.get(s, "meta_access_token") == "EAAsecret123"
        assert settings_store.get_all(s)["meta_access_token"] == "EAAsecret123"
        # an older plain-text value is moved into the encrypted store on first read
        settings_store.set_many(s, {"meta_access_token": ""})
        s.merge(Setting(key="meta_access_token", value="EAAplain")); s.commit()
        assert settings_store.get(s, "meta_access_token") == "EAAplain"
        assert s.get(Setting, "meta_access_token").value == ""
        settings_store.set_many(s, {"meta_access_token": ""})


def test_security_headers_and_no_docs(client):
    r = client.get("/")
    assert r.headers["x-frame-options"] == "DENY" and "script-src 'self'" in r.headers["content-security-policy"]
    assert client.get("/docs").status_code == 404 and client.get("/openapi.json").status_code == 404


def test_team_member_cannot_change_settings(client, auth):
    client.post("/api/users/register", json={"email": "staff2@example.com", "password": "StaffPass1"}, headers=auth)
    tok = client.post("/api/auth/login", json={"email": "staff2@example.com", "password": "StaffPass1"}).json()
    h = {"Authorization": f"Bearer {tok['access_token']}"}
    assert client.post("/api/meta/settings", json={"values": {"store_url": "https://evil.test"}}, headers=h).status_code == 403
    assert client.post("/api/automation", json={"enabled": {}, "max_budget_pct": 50}, headers=h).status_code == 403
    assert client.post("/api/plans/1/launch", headers=h).status_code == 403
    assert client.post("/api/alerts/1/apply", json={"option_index": 0}, headers=h).status_code == 403
    assert client.get("/api/today", headers=h).status_code == 200


def test_upload_must_be_a_real_image(client, auth, plan):
    r = client.post(f"/api/plans/{plan}/creatives/upload?creative_index=0",
                    files={"file": ("a.png", b"<html><script>alert(1)</script>", "image/png")}, headers=auth)
    assert r.status_code == 400


# ---------- third review ----------

def test_owner_routes_cannot_be_bypassed_with_odd_ids(client, auth, plan):
    client.post("/api/users/register", json={"email": "odd@example.com", "password": "OddPass123"}, headers=auth)
    tok = client.post("/api/auth/login", json={"email": "odd@example.com", "password": "OddPass123"}).json()
    h = {"Authorization": f"Bearer {tok['access_token']}"}
    for path in (f"/api/plans/+{plan}", f"/api/plans/{plan}.0", f"/api/plans/%20{plan}"):
        assert client.delete(path, headers=h).status_code in (403, 404, 422), path
    for path in (f"/api/plans/+{plan}/launch", "/api/alerts/1.0/apply", "/api/actions/+1/undo", "/api/launches/+1/complete"):
        assert client.post(path, json={"option_index": 0}, headers=h).status_code in (403, 422), path
    with get_session() as s:
        assert s.get(Plan, plan) is not None


def test_no_automation_on_closed_or_unchecked_launches(plan):
    with get_session() as s:
        s.query(ActionLog).delete()
        s.query(Alert).update({"status": "resolved"}); s.commit()
        settings_store.set_many(s, {"auto_actions": json.dumps({"no_purchases": True})})
        l = _launch(s, plan)
        a = _alert(s, l, plan, "no_purchases", "A1", [{"type": "pause", "level": "adset", "object_id": "A1"}])
        assert a.id in {x.id for x in actions.pending_alerts(s, {l.id}, "act_1")}
        assert actions.pending_alerts(s, set(), "act_1") == []            # its check failed this run
        assert a.id not in {x.id for x in actions.pending_alerts(s, {l.id}, "act_OTHER")}  # another account
        l.status = "archived"; s.commit()
        assert a.id not in {x.id for x in actions.pending_alerts(s, {l.id}, "act_1")}
        settings_store.set_many(s, {"auto_actions": ""})


def test_failing_auto_action_gives_up(plan):
    with get_session() as s:
        s.query(ActionLog).delete()
        s.query(Alert).update({"status": "resolved"}); s.commit()
        settings_store.set_many(s, {"auto_actions": json.dumps({"high_cpa": True})})
        l = _launch(s, plan)
        a = _alert(s, l, plan, "high_cpa", "A1", [{"type": "budget_pct", "level": "adset", "object_id": "A1", "pct": -20}])
        for _ in range(2):
            s.add(ActionLog(alert_id=a.id, launch_id=l.id, mode="auto", spec={}, ok=0, error="x",
                            created_at=datetime.utcnow() - timedelta(days=5)))
        s.commit()
        assert a.id not in {x.id for x in actions.pending_alerts(s, {l.id}, "act_1")}
        settings_store.set_many(s, {"auto_actions": ""})


def test_fit_days_exact_even_when_lopsided():
    phases = [{"days": 1} for _ in range(9)] + [{"days": 100}]
    planner._fit_days(phases, 10)
    assert sum(p["days"] for p in phases) == 10 and all(p["days"] >= 1 for p in phases)
    for total in (1, 3, 7, 30, 90):
        ph = [{"days": d} for d in (0, 5, 50)]
        planner._fit_days(ph, total)
        assert sum(p["days"] for p in ph) == total and all(p["days"] >= 1 for p in ph)


def test_closing_a_launch_resolves_its_alerts(client, auth, plan):
    with get_session() as s:
        l = _launch(s, plan)
        _alert(s, l, plan, "high_cpa", "A1", [])
        from backend.db import PlanOutcome
        s.add(PlanOutcome(launch_id=l.id, plan_id=plan, days_active=10, duration_days=30, spend_usd=120, purchases=2,
                          predicted_expected=10, predicted_low=5, predicted_high=15, predicted_so_far=3, ratio=0.66,
                          verdict="behind"))
        s.commit()
        lid = l.id
    assert client.post(f"/api/launches/{lid}/complete", headers=auth).status_code == 200
    with get_session() as s:
        assert not s.query(Alert).filter(Alert.launch_id == lid, Alert.status != "resolved").count()
