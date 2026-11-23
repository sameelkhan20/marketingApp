"""Executable actions on Meta objects, with guardrails and undo.

Spec: {"type": "pause" | "resume" | "budget_pct", "level": "adset" | "ad", "object_id": str, "pct": float}

Guardrails (always, manual or auto):
- only ad sets and ads are touched, never the campaign
- budget changes are clamped to +/- auto_max_budget_pct (default 20%)
- a budget never drops below ~2x Meta's minimum, and never goes up more than
  the plan's total daily budget
- resume is never automatic
Every execution writes an ActionLog row with the state before, so Undo can revert.
"""

import json
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import settings_store
from .settings_store import ad_account_path
from .benchmarks import get_country
from . import fx
from .db import ActionLog, Alert, Launch, Plan
from .meta_client import MetaClient, MetaError, budget_minor_units

DEFAULT_MAX_PCT = 20.0
AUTOMATABLE_KINDS = ["rebalance", "no_purchases", "high_cpa", "low_ctr", "high_frequency", "low_roas", "scale_candidate",
                     "creative_loser", "creative_winner"]
NEVER_AUTO = {"resume", "refresh_creative"}  # resume changes spend; refresh costs image credits


def describe(spec: dict) -> str:
    t = spec.get("type")
    if t == "pause":
        return f"Pause {spec['level']} {spec['object_id']}"
    if t == "resume":
        return f"Resume {spec['level']} {spec['object_id']}"
    if t == "budget_pct":
        return f"Change {spec['level']} {spec['object_id']} daily budget by {spec['pct']:+.0f}%"
    if t == "clone_winner":
        return f"Clone winning ad {spec['object_id']} into the other ad sets (paused)"
    if t == "move_budget":
        return f"Move {spec.get('pct', 20):.0f}% of ad set {spec['object_id']} daily budget to ad set {spec.get('to', '?')}"
    if t == "refresh_creative":
        return f"Generate a fresh image variant for ad {spec['object_id']} and add it as a paused ad"
    return "No-op"


def concretize(action: dict | None, level: str, object_id: str) -> dict | None:
    """Turn a generic option action into a spec for this alert's object, or None if not applicable."""
    if not action or action.get("type") in (None, "none"):
        return None
    t = action["type"]
    if level not in ("adset", "ad"):
        return None
    if t == "budget_pct" and level != "adset":
        return None
    if t == "move_budget" and level != "adset":
        return None
    if t in ("clone_winner", "refresh_creative") and level != "ad":
        return None
    spec = {"type": t, "level": level, "object_id": object_id}
    if t == "move_budget":
        spec["pct"] = float(action.get("pct", 20))
    if t == "budget_pct":
        spec["pct"] = float(action.get("pct", 0))
        if spec["pct"] == 0:
            return None
    return spec


def automation_settings(session: Session) -> dict:
    vals = settings_store.get_all(session)
    try:
        enabled = json.loads(vals.get("auto_actions") or "{}")
    except json.JSONDecodeError:
        enabled = {}
    try:
        max_pct = float(vals.get("auto_max_budget_pct") or DEFAULT_MAX_PCT)
    except ValueError:
        max_pct = DEFAULT_MAX_PCT
    return {"enabled": {k: bool(enabled.get(k)) for k in AUTOMATABLE_KINDS}, "max_budget_pct": max_pct,
            "kinds": AUTOMATABLE_KINDS}


def execute(session: Session, client: MetaClient, spec: dict, mode: str,
            alert_id: int | None = None, launch_id: int | None = None) -> ActionLog:
    # The log row is added only at the end: adding it first would autoflush and hold SQLite's
    # write lock during the Meta / image-generation calls, blocking every other request.
    log = ActionLog(alert_id=alert_id, launch_id=launch_id, mode=mode, spec=spec, description=describe(spec))
    try:
        if spec["level"] not in ("adset", "ad"):
            raise MetaError("Guardrail: only ad sets and ads can be changed.")
        if spec["type"] in NEVER_AUTO and mode == "auto":
            raise MetaError(f"Guardrail: {spec['type']} is never automatic.")

        if spec["type"] in ("clone_winner", "refresh_creative"):
            created = (_clone_winner if spec["type"] == "clone_winner" else _refresh_creative)(session, client, spec, launch_id)
            log.before = {"created_ads": []}
            log.after = {"created_ads": created}
            log.description += f" -> {len(created)} new paused ad(s): {', '.join(a['name'] for a in created)}" if created \
                else " -> nothing to create"
            log.ok = 1
            session.add(log)
            session.commit()
            return log

        if spec["type"] == "move_budget":
            _move_budget(session, client, spec, launch_id, log)
            log.ok = 1
            session.add(log)
            session.commit()
            return log

        before = client.get_object(spec["object_id"], spec["level"])
        log.before = {"status": before.get("status"), "daily_budget": before.get("daily_budget")}

        if spec["type"] in ("pause", "resume"):
            new_status = "PAUSED" if spec["type"] == "pause" else "ACTIVE"
            if before.get("status") == new_status:
                if mode != "auto":
                    raise MetaError(f"Already {new_status}.")
                # automation wanted it paused and it already is: done, nothing to undo
                log.after = dict(log.before)
                log.description += " (already done)"
                log.ok = 1
                session.add(log)
                session.commit()
                return log
            client.update_object(spec["object_id"], status=new_status)
            log.after = {"status": new_status, "daily_budget": before.get("daily_budget")}

        elif spec["type"] == "budget_pct":
            settings = automation_settings(session)
            pct = max(-settings["max_budget_pct"], min(settings["max_budget_pct"], spec["pct"]))
            current = int(before.get("daily_budget") or 0)
            if current <= 0:
                raise MetaError("Object has no daily budget (campaign budget optimisation?).")
            new_minor = int(round(current * (1 + pct / 100)))
            lo, hi = _budget_bounds(session, client, launch_id)
            if pct < 0:
                if current <= lo:
                    raise MetaError("This ad set is already at the lowest safe budget; pause it instead.")
                new_minor = min(current, max(lo, new_minor))
            else:
                # hi is the plan's daily budget for the whole campaign, not for each ad set
                others = _sibling_budgets(client, launch_id, spec["object_id"], session)
                new_minor = max(current, min(new_minor, hi - others))
            if new_minor == current:
                raise MetaError("Budget unchanged after guardrails (the campaign is at its planned daily budget, "
                                "or this ad set is at the minimum).")
            client.update_object(spec["object_id"], daily_budget=new_minor)
            log.after = {"status": before.get("status"), "daily_budget": str(new_minor)}
            log.description += f" ({current} -> {new_minor} minor units, clamped to +/-{settings['max_budget_pct']:.0f}%)"
        else:
            raise MetaError(f"Unknown action type {spec['type']}")
        log.ok = 1
    except _PartialCreate as ex:
        log.after = {"created_ads": ex.created}
        log.ok, log.error = (1 if ex.created else 0), str(ex.cause)
        log.description += f" -> {len(ex.created)} ad(s) created before an error"
    except MetaError as ex:
        log.ok, log.error = 0, str(ex)
    except Exception as ex:  # never let an unexpected error lose the log
        log.ok, log.error = 0, f"Unexpected error: {ex}"
    session.add(log)
    session.commit()
    return log


def _budget_bounds(session: Session, client: MetaClient, launch_id: int | None) -> tuple[int, int]:
    """(min, max) daily budget in minor units for this launch's account currency."""
    currency = "USD"
    daily_total_usd = 10**9
    country_min = 5
    rate_now = fx.pkr_per_usd()
    if launch_id:
        launch = session.get(Launch, launch_id)
        plan = session.get(Plan, launch.plan_id) if launch else None
        if launch and plan:
            currency = launch.objects.get("currency", "USD")
            rate_now = fx.launch_rate(launch, plan)  # the rate the campaign was built with
            daily_total_usd = plan.budget_pkr / rate_now / max(plan.duration_days, 1)
            try:
                country_min = get_country(plan.country)["min_daily_usd"]
            except KeyError:
                pass
    rate = rate_now if currency == "PKR" else 1.0
    lo = budget_minor_units(country_min * 2 * rate, currency)
    hi = budget_minor_units(daily_total_usd * rate, currency) if daily_total_usd < 10**9 else 10**12
    return lo, max(hi, lo)


def _sibling_budgets(client: MetaClient, launch_id: int | None, object_id: str, session: Session) -> int:
    """Sum of the other ad sets' daily budgets in this launch (minor units)."""
    launch = session.get(Launch, launch_id) if launch_id else None
    total = 0
    for a in (launch.objects.get("adsets", []) if launch else []):
        if a.get("id") and a["id"] != object_id:
            try:
                obj = client.get_object(a["id"], "adset")
            except MetaError:
                continue
            if obj.get("status") != "PAUSED":
                total += int(obj.get("daily_budget") or 0)
    return total


def _launch_and_ad(session: Session, launch_id: int | None, ad_id: str) -> tuple[Launch, Plan, dict]:
    launch = session.get(Launch, launch_id) if launch_id else None
    if not launch:
        raise MetaError("This action needs a launch created by the app.")
    plan = session.get(Plan, launch.plan_id)
    ad = next((a for a in launch.objects.get("ads", []) if a.get("id") == ad_id), None)
    if not ad:
        raise MetaError("Ad was not created by the app, so its creative is unknown.")
    return launch, plan, ad


def _record_ads(session: Session, launch: Launch, new_ads: list[dict]) -> None:
    objs = dict(launch.objects)
    objs["ads"] = list(objs.get("ads", [])) + new_ads
    launch.objects = objs  # reassign so SQLAlchemy sees the JSON change
    session.flush()


def _clone_winner(session: Session, client: MetaClient, spec: dict, launch_id: int | None) -> list[dict]:
    launch, plan, ad = _launch_and_ad(session, launch_id, spec["object_id"])
    act = launch.objects.get("account") or ad_account_path(settings_store.get(session, "meta_ad_account_id"))
    existing = {(a.get("adset_id"), a.get("creative_id")) for a in launch.objects.get("ads", [])}
    created = []
    try:
        for adset in launch.objects.get("adsets", []):
            if adset["id"] == ad["adset_id"] or (adset["id"], ad["creative_id"]) in existing:
                continue
            name = f"{adset['name']} - W{ad.get('creative_index', 0) + 1}"
            new_id = client.create_ad(act, name, adset["id"], ad["creative_id"])
            created.append({"id": new_id, "adset_id": adset["id"], "creative_id": ad["creative_id"],
                            "creative_index": ad.get("creative_index", 0), "name": name, "cloned_from": ad["id"]})
    except MetaError as ex:
        _record_ads(session, launch, created)  # keep what exists on Meta so Undo can remove it
        raise _PartialCreate(created, ex)
    _record_ads(session, launch, created)
    return created


class _PartialCreate(Exception):
    def __init__(self, created: list[dict], cause: Exception):
        super().__init__(str(cause))
        self.created, self.cause = created, cause


def _refresh_creative(session: Session, client: MetaClient, spec: dict, launch_id: int | None) -> list[dict]:
    from . import creatives, imagegen
    from .config import MEDIA_DIR
    from .launcher import CTA_MAP

    launch, plan, ad = _launch_and_ad(session, launch_id, spec["object_id"])
    if not imagegen.status()["configured"]:
        raise MetaError("Image generation is not configured (IMAGE_PROVIDER in .env).")
    vals = settings_store.get_all(session)
    if not vals["meta_page_id"] or not vals["store_url"]:
        raise MetaError("Page id and store URL are needed to create an ad.")
    act = launch.objects.get("account") or ad_account_path(vals["meta_ad_account_id"])
    idx = int(ad.get("creative_index", 0))
    copy = plan.versions[-1].content.get("creatives", [])[idx]

    try:
        row = creatives.generate_for_plan(session, plan, idx)
    except (imagegen.ImageGenError, ValueError) as ex:
        raise MetaError(f"Image generation failed: {ex}")
    row.meta_image_hash = client.upload_image_bytes(act, (MEDIA_DIR / row.file_name).read_bytes(), row.file_name)
    session.commit()

    creative_id = client.create_creative(
        act, name=f"{ad['name']} refresh",
        object_story_spec={"page_id": vals["meta_page_id"], "link_data": {
            "link": vals["store_url"], "image_hash": row.meta_image_hash,
            "message": copy.get("primary_text", ""), "name": copy.get("headline", plan.product),
            "call_to_action": {"type": CTA_MAP.get(copy.get("cta", "").lower(), "SHOP_NOW"),
                               "value": {"link": vals["store_url"]}}}})
    n = sum(1 for a in launch.objects.get("ads", []) if a.get("refreshed_from") == ad["id"]) + 1
    name = f"{ad['name']} - R{n}"
    new_id = client.create_ad(act, name, ad["adset_id"], creative_id)
    created = [{"id": new_id, "adset_id": ad["adset_id"], "creative_id": creative_id, "creative_index": idx,
                "name": name, "refreshed_from": ad["id"], "app_creative_id": row.id}]
    _record_ads(session, launch, created)
    return created


def _move_budget(session: Session, client: MetaClient, spec: dict, launch_id: int | None, log: ActionLog) -> None:
    """Take pct% of one ad set's daily budget and add exactly that amount to another: total unchanged.
    Clamped by the max-change setting and Meta's minimum daily budget."""
    if not spec.get("to") or spec["to"] == spec["object_id"]:
        raise MetaError("No destination ad set for the budget.")
    src = client.get_object(spec["object_id"], "adset")
    dst = client.get_object(spec["to"], "adset")
    s_cur, d_cur = int(src.get("daily_budget") or 0), int(dst.get("daily_budget") or 0)
    if s_cur <= 0 or d_cur <= 0:
        raise MetaError("Both ad sets need their own daily budget (campaign budget optimisation is on?).")
    pct = min(float(spec.get("pct", 20)), automation_settings(session)["max_budget_pct"])
    lo, _hi = _budget_bounds(session, client, launch_id)
    amount = min(int(round(s_cur * pct / 100)), max(s_cur - lo, 0))
    if amount <= 0:
        raise MetaError("The weaker ad set is already at Meta's minimum budget.")
    log.before = {"daily_budget": str(s_cur), "to_budget": str(d_cur)}
    client.update_object(spec["object_id"], daily_budget=s_cur - amount)
    try:
        client.update_object(spec["to"], daily_budget=d_cur + amount)
    except MetaError as ex:
        try:
            client.update_object(spec["object_id"], daily_budget=s_cur)  # put the money back: never lose budget
        except MetaError as ex2:
            raise MetaError(f"{ex}. Putting the budget back also failed ({ex2}): set ad set {spec['object_id']} "
                            f"to {s_cur} minor units in Ads Manager.") from None
        raise
    log.after = {"daily_budget": str(s_cur - amount), "to_budget": str(d_cur + amount)}
    log.description += f" ({amount} minor units moved, {s_cur} -> {s_cur - amount} and {d_cur} -> {d_cur + amount})"


def undo(session: Session, client: MetaClient, log: ActionLog) -> ActionLog:
    if log.undone or not log.ok:
        raise MetaError("Nothing to undo.")
    if log.spec.get("type") in ("clone_winner", "refresh_creative"):
        rev = ActionLog(alert_id=log.alert_id, launch_id=log.launch_id, mode="undo",
                        spec={"type": "undo", "level": "ad", "object_id": log.spec.get("object_id"), "of": log.id},
                        before=log.after, after=log.before, description=f"Undo: {log.description}")
        session.add(rev)
        try:
            for a in log.after.get("created_ads", []):
                client.update_object(a["id"], status="DELETED")
            launch = session.get(Launch, log.launch_id)
            if launch:
                gone = {a["id"] for a in log.after.get("created_ads", [])}
                objs = dict(launch.objects)
                objs["ads"] = [a for a in objs.get("ads", []) if a.get("id") not in gone]
                launch.objects = objs
            log.undone, rev.ok = 1, 1
        except MetaError as ex:
            rev.ok, rev.error = 0, str(ex)
        session.commit()
        return rev
    if log.spec.get("type") == "move_budget":
        rev = ActionLog(alert_id=log.alert_id, launch_id=log.launch_id, mode="undo",
                        spec={"type": "undo", "level": "adset", "object_id": log.spec.get("object_id"), "of": log.id},
                        before=log.after, after=log.before, description=f"Undo: {log.description}")
        session.add(rev)
        try:
            client.update_object(log.spec["to"], daily_budget=int(log.before["to_budget"]))
            client.update_object(log.spec["object_id"], daily_budget=int(log.before["daily_budget"]))
            log.undone, rev.ok = 1, 1
        except MetaError as ex:
            rev.ok, rev.error = 0, str(ex)
        session.commit()
        return rev
    fields = {}
    if log.before.get("status") and log.before["status"] != log.after.get("status"):
        fields["status"] = log.before["status"]
    if log.before.get("daily_budget") and log.before["daily_budget"] != log.after.get("daily_budget"):
        fields["daily_budget"] = int(log.before["daily_budget"])
    rev = ActionLog(alert_id=log.alert_id, launch_id=log.launch_id, mode="undo",
                    spec={"type": "undo", "level": log.spec.get("level"), "object_id": log.spec.get("object_id"), "of": log.id},
                    before=log.after, after=log.before, description=f"Undo: {log.description}")
    session.add(rev)
    try:
        if fields:
            client.update_object(log.spec["object_id"], **fields)
        log.undone = 1
        rev.ok = 1
    except MetaError as ex:
        rev.ok, rev.error = 0, str(ex)
    session.commit()
    return rev


MAX_FAILED_TRIES = 2


def pending_alerts(session: Session, launch_ids: set[int] | None = None, account: str | None = None) -> list[Alert]:
    """Open alerts of enabled kinds that automation has not handled yet, only for launches that are
    still running, were checked without error in this run (launch_ids) and belong to the connected
    ad account. Alerts held back earlier (cooldown, one change per run, tracking) come back here."""
    enabled = [k for k, on in automation_settings(session)["enabled"].items() if on]
    if not enabled or launch_ids is not None and not launch_ids:
        return []
    tries: dict[int, list[int]] = {}
    for r in session.scalars(select(ActionLog).where(ActionLog.mode == "auto", ActionLog.alert_id.is_not(None))):
        tries.setdefault(r.alert_id, []).append(r.ok)
    q = select(Alert).join(Launch, Launch.id == Alert.launch_id).where(
        Alert.status == "open", Alert.kind.in_(enabled),
        Launch.status.not_in(("completed", "archived", "deleted")))
    if launch_ids is not None:
        q = q.where(Alert.launch_id.in_(launch_ids))
    out = []
    for a in session.scalars(q.order_by(Alert.id)):
        t = tries.get(a.id, [])
        if 1 in t or len(t) >= MAX_FAILED_TRIES:
            continue  # done, or it keeps failing: leave it to the owner
        if account and (session.get(Launch, a.launch_id).objects or {}).get("account") not in (None, "", account):
            continue  # made on another ad account
        out.append(a)
    return out


# What automation may do for each alert kind, whatever order the AI put its options in.
AUTO_ALLOWED = {
    "no_purchases": {"pause"}, "high_cpa": {"pause", "budget_cut"}, "low_ctr": {"pause"},
    "high_frequency": {"budget_cut"}, "low_roas": {"budget_cut"}, "scale_candidate": {"budget_raise"},
    "creative_loser": {"pause"}, "creative_winner": {"clone_winner"}, "rebalance": {"move_budget"},
}
# Kinds whose judgement depends on purchase data: never act on them while the pixel looks broken.
NEEDS_TRACKING = {"no_purchases", "high_cpa", "low_roas", "scale_candidate", "rebalance", "creative_winner", "creative_loser"}
PRIORITY = {"pause": 0, "move_budget": 1, "budget_pct": 2, "clone_winner": 3}
COOLDOWN = timedelta(hours=72)


def _kind_of(spec: dict) -> str:
    if spec["type"] == "budget_pct":
        return "budget_cut" if spec.get("pct", 0) < 0 else "budget_raise"
    return spec["type"]


def _recently_changed(session: Session, object_id: str) -> bool:
    since = datetime.utcnow() - COOLDOWN
    rows = session.scalars(select(ActionLog).where(ActionLog.created_at >= since, ActionLog.ok == 1,
                                                   ActionLog.mode != "undo")).all()
    return any(r.spec.get("object_id") == object_id or r.spec.get("to") == object_id for r in rows)


def auto_apply(session: Session, client: MetaClient, alerts: list[Alert]) -> list[ActionLog]:
    """For each new alert whose kind is enabled, run the first option automation is allowed to run.
    Guardrails: one change per ad set or ad per run, none on an object changed in the last 72 hours,
    and nothing that depends on purchases while a tracking alert is open for that launch."""
    settings = automation_settings(session)
    # only real tracking problems (not "AddToCart missing") block, and only actions that add spend or
    # judge winners: pausing a money-losing ad set is always allowed, it only reduces spend
    broken_tracking = {a.launch_id for a in session.scalars(
        select(Alert).where(Alert.kind == "tracking", Alert.status != "resolved",
                            Alert.severity.in_(("critical", "warning"))))}
    picks: dict[str, tuple[Alert, dict]] = {}
    for a in alerts:
        if not a or not settings["enabled"].get(a.kind):
            continue
        allowed = AUTO_ALLOWED.get(a.kind, set())
        if a.kind in NEEDS_TRACKING and a.launch_id in broken_tracking:
            allowed = allowed & {"pause"}
        spec = next((o["spec"] for o in a.suggestions or [] if o.get("spec") and o["spec"]["type"] not in NEVER_AUTO
                     and _kind_of(o["spec"]) in allowed and o["spec"].get("object_id") == a.object_id), None)
        if not spec:
            continue
        held = picks.get(spec["object_id"])
        if held and PRIORITY.get(held[1]["type"], 9) <= PRIORITY.get(spec["type"], 9):
            continue
        picks[spec["object_id"]] = (a, spec)
    logs: list[ActionLog] = []
    touched: set[str] = set()
    for a, spec in sorted(picks.values(), key=lambda p: PRIORITY.get(p[1]["type"], 9)):
        targets = {spec["object_id"], spec.get("to")} - {None}
        if targets & touched or any(_recently_changed(session, t) for t in targets):
            continue
        touched |= targets
        logs.append(execute(session, client, spec, "auto", alert_id=a.id, launch_id=a.launch_id))
    return logs
