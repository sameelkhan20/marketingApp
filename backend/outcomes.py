"""Learning from outcomes.

1. Plan outcomes: predicted (from the estimate) vs actual purchases, prorated by days
   active. Refreshed on each monitor run; finalised when the campaign completes.
2. Calibration: per-country factor = actual / predicted from completed launches,
   applied to future estimates (clamped 0.5-2.0, blended with sample count).
3. Rules from results: at completion the best / worst ad sets and creatives become
   learned rules (Claude-written when available, deterministic otherwise), and the
   weights of existing rules move with the verdict.
4. Action outcomes: 3+ days after a pause / budget change, the object's CPA (or the
   campaign's, for a pause) before vs after -> improved / neutral / worse.
"""

import json
import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import (ActionLog, ActionOutcome, Calibration, Launch, LearnedRule, MetricSnapshot, Plan, PlanOutcome)
from .meta_sync import _purchases, to_usd

from . import fx
log = logging.getLogger("outcomes")

ACTION_EVAL_AFTER_DAYS = 7  # snapshots hold 7-day windows: wait a full window after the change
MIN_DAYS_FOR_CALIBRATION = 7


# ---------- plan outcome (every monitor run) ----------

def update_plan_outcome(session: Session, launch: Launch, plan: Plan, lifetime: dict, currency: str,
                        days_active: int, campaign_status: str) -> PlanOutcome:
    est = plan.estimate or {}
    spend = to_usd(float(lifetime.get("spend", 0) or 0), currency)
    purchases, revenue = _purchases(lifetime)
    revenue = to_usd(revenue, currency)

    expected = int(est.get("orders_expected", 0))
    raw = float((est.get("assumptions") or {}).get("orders_raw") or expected)
    budget_usd = float(est.get("budget_usd") or plan.budget_pkr / fx.plan_rate(plan))
    day_frac = min(days_active / max(plan.duration_days, 1), 1.0)
    spend_frac = min(spend / budget_usd, 1.0) if budget_usd > 0 else day_frac
    frac = spend_frac if spend > 0 else day_frac
    predicted_so_far = raw * frac
    ratio = purchases / predicted_so_far if predicted_so_far > 0 else 0.0

    if days_active < 2 or spend <= 0 or predicted_so_far < 0.5:
        verdict = "no_data"
    elif ratio >= 1.15:
        verdict = "ahead"
    elif ratio >= 0.75:
        verdict = "on_track"
    else:
        verdict = "behind"

    po = session.get(PlanOutcome, launch.id)
    if not po:
        po = PlanOutcome(launch_id=launch.id, plan_id=plan.id)
        session.add(po)
    if po.completed:
        return po
    po.updated_at = datetime.utcnow()
    po.days_active, po.duration_days = days_active, plan.duration_days
    po.spend_usd, po.purchases, po.revenue_usd = round(spend, 2), purchases, round(revenue, 2)
    po.predicted_expected = expected
    po.predicted_low, po.predicted_high = int(est.get("orders_low", 0)), int(est.get("orders_high", 0))
    po.predicted_so_far, po.ratio, po.verdict = round(predicted_so_far, 2), round(ratio, 2), verdict

    # A temporary pause must not finalise the launch; only the plan's end, or the campaign being
    # archived/deleted, does. Early closing is the explicit "Close & learn" button.
    finished = days_active >= plan.duration_days or campaign_status in ("ARCHIVED", "DELETED")
    if finished and spend > 0:
        complete_launch(session, launch, plan, po)
    return po


# ---------- completion: calibration + rules ----------

def complete_launch(session: Session, launch: Launch, plan: Plan, po: PlanOutcome) -> None:
    # Read + Claude call first, without flushing pending writes, so the SQLite write lock is not
    # held during the network call.
    with session.no_autoflush:
        perf = _performance_summary(session, launch)
        rules = _rules_from_results(plan, po, perf)
    po.completed = 1
    launch.status = "completed"  # the monitor stops polling it
    from .db import Alert
    for a in session.scalars(select(Alert).where(Alert.launch_id == launch.id, Alert.status != "resolved")):
        a.status = "resolved"  # automation never acts on a finished campaign
    used = float(((plan.estimate or {}).get("assumptions") or {}).get("calibration_factor") or 1.0)
    from_history = str((plan.estimate or {}).get("source", "")).startswith("account_history")
    # account history already reflects real results: correcting it again would double count
    if not from_history and po.days_active >= MIN_DAYS_FOR_CALIBRATION and po.predicted_so_far >= 0.5:
        _update_calibration(session, plan.country, po.ratio * used)
    po.summary = perf["text"]
    # at most 6 result rules per market: the new ones replace the oldest, weakest ones
    tag = f"[result {plan.country} {plan.niche}]"
    with session.no_autoflush:
        same = sorted((x for x in session.scalars(select(LearnedRule).where(LearnedRule.active == 1))
                       if x.rule.startswith(tag)), key=lambda x: (x.weight, x.id))
    for x in same[:max(0, len(same) + len(rules) - 6)]:
        x.active = 0
    for r in rules:
        session.add(LearnedRule(rule=r, source_plan_id=plan.id, weight=1.2))
    po.rules_added = rules
    _reweight_rules(session, po.verdict, plan)
    session.commit()
    log.info("launch %s completed: verdict %s, %d rules", launch.id, po.verdict, len(rules))


def _update_calibration(session: Session, country: str, ratio: float | None) -> None:
    if ratio is None:
        return
    ratio = max(0.5, min(2.0, ratio))  # a launch with no sales counts as the lowest ratio, not as nothing
    cal = session.get(Calibration, country.upper())
    if not cal:
        cal = Calibration(country=country.upper(), factor=1.0, samples=0)
        session.add(cal)
    # Mean of a 1.0 prior (one pseudo-sample) and every observed ratio, each weighted equally.
    n = cal.samples
    sum_ratios = cal.factor * (n + 1) - 1.0
    cal.factor = round((1.0 + sum_ratios + ratio) / (n + 2), 3)
    cal.samples = n + 1
    cal.updated_at = datetime.utcnow()


def calibration_factor(session: Session, country: str) -> tuple[float, int]:
    cal = session.get(Calibration, country.upper())
    return (cal.factor, cal.samples) if cal else (1.0, 0)


def _performance_summary(session: Session, launch: Launch) -> dict:
    """Best/worst ad sets and ads from the latest snapshot of this launch."""
    latest_at = session.scalar(select(MetricSnapshot.taken_at).where(MetricSnapshot.launch_id == launch.id)
                               .order_by(MetricSnapshot.taken_at.desc()).limit(1))
    rows = session.scalars(select(MetricSnapshot).where(MetricSnapshot.launch_id == launch.id,
                                                        MetricSnapshot.taken_at == latest_at)).all() if latest_at else []
    out = {"adsets": [], "ads": [], "text": ""}
    for lvl, key in (("adset", "adsets"), ("ad", "ads")):
        items = [r for r in rows if r.level == lvl and r.spend_usd > 0]
        items.sort(key=lambda r: (r.cpa_usd if r.purchases else 10**9, -r.ctr))
        out[key] = [{"name": r.name, "spend_usd": r.spend_usd, "purchases": r.purchases, "cpa_usd": r.cpa_usd,
                     "ctr": r.ctr, "roas": r.roas} for r in items]
    parts = []
    if out["adsets"]:
        b = out["adsets"][0]
        parts.append(f"Best ad set: {b['name']} ({b['purchases']} purchases, CPA ${b['cpa_usd']}, CTR {b['ctr']:.2%})")
        if len(out["adsets"]) > 1:
            w = out["adsets"][-1]
            parts.append(f"weakest: {w['name']} ({w['purchases']} purchases, ${w['spend_usd']} spent)")
    if out["ads"]:
        b = out["ads"][0]
        parts.append(f"best ad: {b['name']} (CTR {b['ctr']:.2%}, {b['purchases']} purchases)")
    out["text"] = "; ".join(parts)
    return out


def _rules_from_results(plan: Plan, po: PlanOutcome, perf: dict) -> list[str]:
    from . import llm
    if llm.configured():
        try:
            got = _ai_rules(plan, po, perf)
            if got:
                return got
        except Exception as ex:
            log.warning("AI outcome rules failed: %s", ex)
    rules = []
    tag = f"[result {plan.country} {plan.niche}]"
    if perf["adsets"] and perf["adsets"][0]["purchases"] >= 3:
        b = perf["adsets"][0]
        rules.append(f"{tag} '{b['name']}' audience delivered the best CPA (${b['cpa_usd']}); give it the largest share next time.")
    if len(perf["adsets"]) > 1 and perf["adsets"][-1]["purchases"] == 0 and perf["adsets"][-1]["spend_usd"] > 0:
        w = perf["adsets"][-1]
        rules.append(f"{tag} '{w['name']}' audience spent ${w['spend_usd']} with no purchases; drop it or test it with a small budget only.")
    if po.verdict == "behind":
        rules.append(f"{tag} Results came in at {po.ratio:.0%} of the estimate; plan more conservatively or increase testing budget for new creatives.")
    elif po.verdict == "ahead":
        rules.append(f"{tag} Results beat the estimate ({po.ratio:.0%}); this setup can be scaled faster.")
    return rules[:3]


def _ai_rules(plan: Plan, po: PlanOutcome, perf: dict) -> list[str]:
    from . import llm  # raises on budget or provider failure; the caller falls back to fixed rules
    schema = {"type": "object", "properties": {"rules": {"type": "array", "items": {"type": "string"}}},
              "required": ["rules"], "additionalProperties": False}
    plan_content = plan.versions[-1].content
    prompt = (
        "A Meta campaign planned by this system has finished. Write 1-3 short rules (one sentence each, "
        f"prefixed with '[result {plan.country} {plan.niche}]') that the planner should follow next time for this "
        "country and niche, based only on what actually performed. Be specific about audiences, creative "
        "formats/hooks and budget split. No generic advice.\n\n"
        f"PLAN: {json.dumps({k: plan_content.get(k) for k in ('ad_sets', 'creatives', 'phases')})}\n"
        f"OUTCOME: predicted {po.predicted_low}-{po.predicted_high} (expected {po.predicted_expected}) orders over "
        f"{po.duration_days} days; actual {po.purchases} purchases in {po.days_active} days, ${po.spend_usd} spent, "
        f"verdict {po.verdict} ({po.ratio:.0%} of prorated estimate).\n"
        f"PERFORMANCE: {json.dumps(perf)}"
    )
    rules = llm.structured("You turn finished Meta campaign results into planning rules.", prompt, schema,
                           "Result rules", effort="medium", max_tokens=2000)["rules"]
    tag = f"[result {plan.country} {plan.niche}]"
    return [r if r.startswith("[result") else f"{tag} {r}" for r in (str(x).strip() for x in rules) if r][:3]


def _reweight_rules(session: Session, verdict: str, plan: Plan) -> None:
    """Only rules that shaped this plan (existed when it was generated) share its result."""
    from .learning import rules_in_effect_for
    from .learning import RETIRE_BELOW, applies_to
    delta = {"ahead": 0.2, "on_track": 0.1, "behind": -0.2}.get(verdict, 0)
    if not delta:
        return
    for r in rules_in_effect_for(session, plan.created_at):
        if r.source_plan_id == plan.id and r.rule.startswith("[result"):
            continue  # rules this very launch just produced
        if not applies_to(r.rule, plan.country, plan.niche):
            continue  # a rule for another market did not shape this plan
        r.weight = round(min(3.0, max(0.1, r.weight + delta)), 2)
        if r.weight < RETIRE_BELOW:
            r.active = 0  # kept losing: stop using it (it stays visible as inactive)


# ---------- action outcomes ----------

def evaluate_action_outcomes(session: Session) -> list[ActionOutcome]:
    """For actions older than ACTION_EVAL_AFTER_DAYS without an outcome, compare the object's
    CPA before and after (campaign CPA for pauses, since the paused object has no 'after')."""
    cutoff = datetime.utcnow() - timedelta(days=ACTION_EVAL_AFTER_DAYS)
    done_ids = {o.action_id for o in session.scalars(select(ActionOutcome))}
    actions = [a for a in session.scalars(select(ActionLog).where(ActionLog.ok == 1, ActionLog.undone == 0,
                                                                  ActionLog.mode != "undo", ActionLog.created_at <= cutoff))
               if a.id not in done_ids and a.launch_id]
    results = []
    for a in actions:
        spec = a.spec or {}
        if spec.get("type") == "pause":
            level, object_id = "campaign", None  # campaign-level: did pausing help overall?
        else:
            level, object_id = spec.get("level"), spec.get("object_id")

        def snap(before: bool):
            q = select(MetricSnapshot).where(MetricSnapshot.launch_id == a.launch_id, MetricSnapshot.level == level)
            if object_id:
                q = q.where(MetricSnapshot.object_id == object_id)
            q = (q.where(MetricSnapshot.taken_at <= a.created_at).order_by(MetricSnapshot.taken_at.desc()) if before
                 else q.where(MetricSnapshot.taken_at >= a.created_at + timedelta(days=ACTION_EVAL_AFTER_DAYS))
                 .order_by(MetricSnapshot.taken_at.asc()))  # the first full window after, not the latest
            return session.scalar(q.limit(1))

        b, af = snap(True), snap(False)
        if not b or not af:
            continue  # not enough data yet; try again next run
        oc = ActionOutcome(action_id=a.id, metric="cpa_usd", before=b.cpa_usd, after=af.cpa_usd,
                           purchases_before=b.purchases, purchases_after=af.purchases)
        if b.purchases == 0 and af.purchases == 0:
            oc.verdict, oc.note = "no_data", "No purchases before or after."
        elif b.purchases == 0 and af.purchases > 0:
            oc.verdict, oc.note = "improved", "Purchases started after the action."
        elif af.purchases == 0:
            oc.verdict, oc.note = "worse", "Purchases stopped after the action."
        elif b.cpa_usd <= 0 or af.cpa_usd <= 0:
            oc.verdict, oc.note = "no_data", "Purchases without spend in the window (late attribution)."
        else:
            change = (af.cpa_usd - b.cpa_usd) / b.cpa_usd
            scale_up = spec.get("type") == "budget_pct" and spec.get("pct", 0) > 0
            if scale_up and af.purchases > b.purchases and change <= 0.15:
                oc.verdict, oc.note = "improved", f"More purchases ({b.purchases} -> {af.purchases}) with CPA {change:+.0%}."
            elif change <= -0.10:
                oc.verdict, oc.note = "improved", f"CPA {change:+.0%}."
            elif change >= 0.15:
                oc.verdict, oc.note = "worse", f"CPA {change:+.0%}."
            else:
                oc.verdict, oc.note = "neutral", f"CPA {change:+.0%}."
        session.add(oc)
        results.append(oc)
    session.commit()
    return results


def action_stats(session: Session) -> dict:
    """Success rate per action kind, shown in the Automation card to inform what to automate."""
    rows = session.execute(select(ActionOutcome, ActionLog).join(ActionLog, ActionLog.id == ActionOutcome.action_id)).all()
    stats: dict[str, dict] = {}
    for oc, a in rows:
        kind = f"{a.spec.get('type')}{'+' if a.spec.get('pct', 0) > 0 else '-' if a.spec.get('type') == 'budget_pct' else ''}"
        s = stats.setdefault(kind, {"improved": 0, "neutral": 0, "worse": 0, "no_data": 0})
        s[oc.verdict] = s.get(oc.verdict, 0) + 1
    return stats
