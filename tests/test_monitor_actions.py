import json
from datetime import datetime, timedelta

from sqlalchemy import select

from backend import actions, monitor, outcomes, settings_store
from backend.db import ActionLog, Alert, Launch, MetricSnapshot, Plan, PlanOutcome, get_session
from backend.rules import TEMPLATE_SUGGESTIONS, evaluate, evaluate_creatives
from tests.fakes import FakeMeta


def _launch(session, plan_id):
    l = Launch(plan_id=plan_id, campaign_id=f"FAKE_{plan_id}", objects={
        "account": "act_1", "currency": "USD", "notes": [], "ads_manager_url": "#",
        "adsets": [{"id": "A1", "name": "Broad"}, {"id": "A2", "name": "LAL"}],
        "ads": [{"id": "AD1", "adset_id": "A1", "creative_id": "CR1", "creative_index": 0, "name": "Broad - C1"},
                {"id": "AD2", "adset_id": "A1", "creative_id": "CR2", "creative_index": 1, "name": "Broad - C2"}]})
    session.add(l)
    session.get(Plan, plan_id).status = "launched"
    session.commit()
    return l


def test_rules_engine_min_data_and_kinds():
    targets = {"max_cpa_usd": 60, "min_ctr": 0.01, "max_frequency": 3.0, "min_roas": 1.5}
    m = [{"level": "campaign", "object_id": "C", "name": "camp", "spend_usd": 100, "impressions": 9000, "clicks": 120,
          "purchases": 1, "revenue_usd": 90, "frequency": 1.4, "ctr": 0.0133, "cpa_usd": 100, "roas": 0.9},
         {"level": "adset", "object_id": "A1", "name": "Broad", "spend_usd": 70, "impressions": 6000, "clicks": 40,
          "purchases": 0, "revenue_usd": 0, "frequency": 3.4, "ctr": 0.0067, "cpa_usd": 0, "roas": 0}]
    ads = [{"id": "AD9", "name": "x", "effective_status": "DISAPPROVED", "issues_info": [{"error_summary": "Trademark"}]}]
    kinds = sorted(f["kind"] for f in evaluate(m, targets, 40, 5, "ACTIVE", ads))
    assert kinds == ["delivery", "high_cpa", "high_frequency", "low_ctr", "no_purchases", "under_pacing"]
    assert evaluate(m, targets, 40, 5, "PAUSED", ads) == []


def test_creative_winner_loser():
    def ad(oid, parent, name, imp, clk, pur, spend):
        return {"level": "ad", "object_id": oid, "parent_id": parent, "name": name, "effective_status": "ACTIVE",
                "spend_usd": spend, "impressions": imp, "clicks": clk, "purchases": pur, "revenue_usd": 0,
                "frequency": 1, "ctr": clk / imp, "cpa_usd": spend / pur if pur else 0, "roas": 0}
    f = evaluate_creatives([ad("AD1", "A1", "C1", 3000, 40, 4, 100), ad("AD2", "A1", "C2", 3000, 30, 0, 80),
                            ad("AD4", "A2", "x", 500, 10, 0, 10), ad("AD5", "A2", "y", 500, 10, 0, 10)], 60)
    assert sorted((x["kind"], x["object_id"]) for x in f) == [("creative_loser", "AD2"), ("creative_winner", "AD1")]


def test_monitor_pipeline_dedupe_and_outcome(plan):
    with get_session() as s:
        l = _launch(s, plan)
        fake = FakeMeta()
        r1 = monitor.check_launch(s, fake, l, "USD")
        r2 = monitor.check_launch(s, fake, l, "USD")
        assert r1["new_alerts"] and r2["new_alerts"] == []
        alerts = s.scalars(select(Alert).where(Alert.launch_id == l.id)).all()
        assert all(a.times_seen == 2 for a in alerts)
        kinds = {a.kind for a in alerts}
        assert {"over_pacing", "creative_winner", "creative_loser"} <= kinds
        po = s.get(PlanOutcome, l.id)
        assert po and po.purchases == 9 and po.verdict in ("ahead", "on_track", "behind")
        # paused campaign -> alerts resolve
        fake.campaign_effective_status = "PAUSED"
        monitor.check_launch(s, fake, l, "USD")
        assert all(a.status == "resolved" for a in s.scalars(select(Alert).where(Alert.launch_id == l.id)))


def test_actions_guardrails_undo_and_auto(plan):
    with get_session() as s:
        l = _launch(s, plan)
        fake = FakeMeta()
        log = actions.execute(s, fake, {"type": "pause", "level": "adset", "object_id": "A1"}, "manual", launch_id=l.id)
        assert log.ok and fake.objects["A1"]["status"] == "PAUSED"
        assert actions.undo(s, fake, log).ok and fake.objects["A1"]["status"] == "ACTIVE"

        settings_store.set_many(s, {"auto_max_budget_pct": "10"})
        # plan daily total is $11.90 (1190 cents) for the WHOLE campaign: with A1 at 1000 running,
        # A2 has no headroom to grow
        raise_blocked = actions.execute(s, fake, {"type": "budget_pct", "level": "adset", "object_id": "A2", "pct": 20}, "manual", launch_id=l.id)
        assert not raise_blocked.ok and fake.objects["A2"]["daily_budget"] == "1000"
        fake.objects["A1"]["status"] = "PAUSED"  # paused ad sets do not count
        log2 = actions.execute(s, fake, {"type": "budget_pct", "level": "adset", "object_id": "A2", "pct": 20}, "manual", launch_id=l.id)
        assert log2.ok and fake.objects["A2"]["daily_budget"] == "1100"      # clamped to +10%
        log3 = actions.execute(s, fake, {"type": "budget_pct", "level": "adset", "object_id": "A2", "pct": 20}, "manual", launch_id=l.id)
        assert log3.ok and fake.objects["A2"]["daily_budget"] == "1190"      # capped at plan daily total
        # a cut never raises: at the 2x-minimum floor (1000 for the US) it is refused, not pushed up
        fake.objects["A2"]["daily_budget"] = "900"
        cut = actions.execute(s, fake, {"type": "budget_pct", "level": "adset", "object_id": "A2", "pct": -20}, "manual", launch_id=l.id)
        assert not cut.ok and fake.objects["A2"]["daily_budget"] == "900"
        fake.objects["A1"]["status"] = "ACTIVE"

        assert not actions.execute(s, fake, {"type": "pause", "level": "campaign", "object_id": "C"}, "manual").ok
        assert not actions.execute(s, fake, {"type": "resume", "level": "adset", "object_id": "A1"}, "auto").ok
        assert not actions.execute(s, fake, {"type": "refresh_creative", "level": "ad", "object_id": "AD1"}, "auto", launch_id=l.id).ok

        clone = actions.execute(s, fake, {"type": "clone_winner", "level": "ad", "object_id": "AD1"}, "manual", launch_id=l.id)
        assert clone.ok and fake.ads_created[0]["adset_id"] == "A2"
        assert actions.undo(s, fake, clone).ok and ("NEW1", {"status": "DELETED"}) in fake.updates

        # auto-apply only enabled kinds
        a = Alert(launch_id=l.id, plan_id=plan, fingerprint="x", severity="warning", kind="no_purchases",
                  object_level="adset", object_id="A1", object_name="Broad", message="m", metrics={},
                  suggestions=monitor.with_specs(TEMPLATE_SUGGESTIONS["no_purchases"], "adset", "A1"))
        s.add(a); s.commit()
        settings_store.set_many(s, {"auto_actions": json.dumps({"high_cpa": True})})
        assert actions.auto_apply(s, fake, [a]) == []
        settings_store.set_many(s, {"auto_actions": json.dumps({"no_purchases": True})})
        # A1 was changed by hand minutes ago: automation waits 72 hours before touching it again
        assert actions.auto_apply(s, fake, [a]) == []
        for row in s.query(ActionLog).all():
            row.created_at = datetime.utcnow() - timedelta(days=4)
        s.commit()
        assert len(actions.auto_apply(s, fake, [a])) == 1 and fake.objects["A1"]["status"] == "PAUSED"
        # and not twice in a row
        fake.objects["A1"]["status"] = "ACTIVE"
        assert actions.auto_apply(s, fake, [a]) == []
        settings_store.set_many(s, {"auto_actions": "", "auto_max_budget_pct": ""})


def test_action_outcome_and_completion(plan):
    with get_session() as s:
        l = _launch(s, plan)
        t0 = datetime.utcnow() - timedelta(days=10)
        def snap(at, level, oid, name, spend, pur, ctr=0.012):
            s.add(MetricSnapshot(launch_id=l.id, taken_at=at, level=level, object_id=oid, name=name, spend_usd=spend,
                                 impressions=10000, clicks=120, purchases=pur, revenue_usd=pur * 120, frequency=1.5, ctr=ctr,
                                 cpa_usd=round(spend / pur, 2) if pur else 0, roas=1))
        snap(t0, "adset", "A2", "LAL", 40, 1); snap(t0 + timedelta(days=9), "adset", "A2", "LAL", 150, 7)
        snap(t0 + timedelta(days=9), "adset", "A1", "Broad", 150, 0, 0.009)
        s.add(ActionLog(launch_id=l.id, created_at=t0 + timedelta(days=1), mode="manual",
                        spec={"type": "budget_pct", "level": "adset", "object_id": "A2", "pct": 20}, ok=1))
        s.commit()
        ocs = outcomes.evaluate_action_outcomes(s)
        assert len(ocs) == 1 and ocs[0].verdict == "improved"

        p = s.get(Plan, plan)
        po = outcomes.update_plan_outcome(s, l, p, FakeMeta().campaign_lifetime("x"), "USD", 30, "ACTIVE")
        s.commit()
        assert po.completed and po.verdict == "ahead" and po.rules_added
        f, n = outcomes.calibration_factor(s, "US")
        assert n >= 1 and 1.0 < f <= 2.0
