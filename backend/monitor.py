"""Monitoring job: pull live metrics for every launched plan, evaluate the
rules, store snapshots, create/refresh alerts, and attach suggestions.

Runs on a timer (MONITOR_INTERVAL_HOURS) and on demand from the dashboard.
Email delivery hooks in via notify.py (Phase 5).
"""

import json
import logging
import threading
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import settings_store
from . import fx
from .config import MONITOR_INTERVAL_HOURS
from .db import Alert, Launch, MetricSnapshot, Plan, get_session
from .meta_client import paused as meta_paused
from .meta_client import MetaRateLimited, MetaClient, MetaError
from .actions import auto_apply, concretize
from .notify import send_alert_email, send_digest, status as notify_status
from .outcomes import evaluate_action_outcomes, update_plan_outcome
from .rules import TEMPLATE_SUGGESTIONS, evaluate, normalise
from .settings_store import ad_account_path

log = logging.getLogger("monitor")

_state = {"last_run": None, "last_result": None, "running": False}


def state() -> dict:
    return dict(_state)


# ---------- one launch ----------

def check_launch(session: Session, client: MetaClient, launch: Launch, currency: str) -> dict:
    plan: Plan = session.get(Plan, launch.plan_id)
    content = plan.versions[-1].content
    targets = content.get("monitoring_targets", {})

    camp = client.campaign_status(launch.campaign_id)
    adsets, ads = client.campaign_objects(launch.campaign_id)
    names = {o["id"]: o["name"] for o in adsets + ads}
    statuses = {o["id"]: o.get("effective_status", "") for o in adsets + ads}
    names[launch.campaign_id] = camp.get("name", "")
    statuses[launch.campaign_id] = camp.get("effective_status", "")

    insights = client.campaign_insights_7d(launch.campaign_id)
    metrics: list[dict] = []
    for level, rows in insights.items():
        for r in rows:
            if level == "campaign":
                r = {**r, "campaign_id": launch.campaign_id}
            m = normalise(r, currency, level, names, statuses)
            if level != "campaign" and m["object_id"] not in statuses:
                m["effective_status"] = "DELETED"  # not returned by the edges any more
            metrics.append(m)
    # Meta returns no insights row at all for a campaign that delivered nothing: make one,
    # so a stuck ACTIVE campaign raises "no delivery" instead of staying silent.
    if camp.get("effective_status") == "ACTIVE" and not any(m["level"] == "campaign" for m in metrics):
        metrics.append(normalise({"campaign_id": launch.campaign_id, "spend": "0", "impressions": "0"},
                                 currency, "campaign", names, statuses))

    now = datetime.utcnow()
    for m in metrics:
        session.add(MetricSnapshot(launch_id=launch.id, taken_at=now, **{k: v for k, v in m.items() if k != "parent_id"}))

    # Days are counted from when the campaign actually started running, not from creation:
    # launches are created paused and may be activated days later.
    try:
        lifetime = client.campaign_lifetime(launch.campaign_id)
    except Exception as ex:
        log.warning("lifetime insights failed for launch %s: %s", launch.id, ex)
        lifetime = None
    was_active = bool((launch.objects or {}).get("activated_at"))
    days_active = _days_active(launch, camp, lifetime, now)
    if not was_active and (launch.objects or {}).get("activated_at"):
        _restart_clock(client, launch, plan)
    daily_usd = plan.budget_pkr / fx.launch_rate(launch, plan) / max(plan.duration_days, 1)

    findings = evaluate(metrics, targets, daily_usd, days_active, camp.get("effective_status", ""), ads)
    tracking = _tracking_findings(session, client, launch, metrics, camp.get("effective_status", ""))
    findings += tracking or []
    new_alerts = _upsert_alerts(session, launch, plan.id, findings, keep_kinds={"tracking"} if tracking is None else set())

    launch.status = camp.get("effective_status", launch.status).lower()
    session.commit()

    # Predicted vs actual (lifetime totals), calibration + rules on completion
    if lifetime is not None:
        try:
            update_plan_outcome(session, launch, plan, lifetime, currency, days_active, camp.get("effective_status", ""))
            session.commit()
        except Exception as ex:  # outcome tracking must never break monitoring
            session.rollback()
            log.warning("outcome update failed for launch %s: %s", launch.id, ex)
    return {"launch_id": launch.id, "campaign_id": launch.campaign_id,
            "status": launch.status, "metrics": len(metrics), "findings": len(findings),
            "new_alerts": [a.id for a in new_alerts]}


INACTIVE_LAUNCH = ("archived", "completed", "deleted")


def _tracking_findings(session: Session, client, launch: Launch, metrics: list[dict], status: str) -> list[dict]:
    """Pixel problems as campaign-level findings (only while the campaign runs)."""
    from .tracking import problems
    from . import brands
    try:
        pixel = brands.settings_for(session, brands.launch_brand(session, launch))["meta_pixel_id"]
    except brands.BrandError:
        return None
    if not pixel or status not in ("ACTIVE", "IN_PROCESS", "WITH_ISSUES") or not hasattr(client, "pixel_health"):
        return []
    try:
        health = client.pixel_health(pixel)
    except MetaError as ex:
        log.warning("pixel health failed: %s", ex)
        return None  # unknown: not the same as "healthy"
    camp = next((m for m in metrics if m["level"] == "campaign"), {})
    out = []
    for p in problems(health, int(camp.get("clicks", 0))):
        key = p.get("key") or "".join(ch for ch in p["text"].lower()[:24] if ch.isalnum())
        out.append({"kind": "tracking", "severity": p["severity"], "level": "campaign",
                    "object_id": f"{launch.campaign_id}#pixel-{key}", "object_name": "Pixel", "message": p["text"],
                    "metrics": {"events_7d": health.get("counts", {})}})
    return out


def _restart_clock(client, launch: Launch, plan: Plan) -> None:
    """Ad sets were given an end date at creation, but campaigns start paused: when the owner switches
    the campaign on, the plan's days start then, so move the end date once."""
    import time as _time
    end = int(_time.time()) + plan.duration_days * 86400
    for a in (launch.objects or {}).get("adsets", []):
        try:
            client.update_object(a["id"], end_time=end)
        except MetaError as ex:
            log.warning("could not move the end date of ad set %s: %s", a.get("id"), ex)
            return
    objs = dict(launch.objects or {})
    objs["ends_at"] = end
    launch.objects = objs


def _days_active(launch: Launch, camp: dict, lifetime: dict | None, now: datetime) -> int:
    """Days since the campaign started delivering. Start = first insights day, or the first
    time the monitor saw it ACTIVE (recorded in launch.objects), whichever is earlier."""
    from datetime import date as _date

    objs = dict(launch.objects or {})
    if camp.get("effective_status") == "ACTIVE" and not objs.get("activated_at"):
        objs["activated_at"] = now.isoformat()
        launch.objects = objs
    starts = []
    if objs.get("activated_at"):
        starts.append(datetime.fromisoformat(objs["activated_at"]).date())
    if lifetime and lifetime.get("date_start") and float(lifetime.get("spend", 0) or 0) > 0:
        starts.append(_date.fromisoformat(lifetime["date_start"][:10]))
    if not starts:
        return 0
    return max((now.date() - min(starts)).days, 0)


def _upsert_alerts(session: Session, launch: Launch, plan_id: int, findings: list[dict],
                   keep_kinds: set[str] = frozenset()) -> list[Alert]:
    open_alerts = {a.fingerprint: a for a in session.scalars(
        select(Alert).where(Alert.launch_id == launch.id, Alert.status != "resolved"))}
    seen: set[str] = set()
    new: list[Alert] = []
    for f in findings:
        fp = f"{f['kind']}:{f['level']}:{f['object_id']}"
        seen.add(fp)
        if fp in open_alerts:
            a = open_alerts[fp]
            a.message, a.metrics, a.severity = f["message"], f["metrics"], f["severity"]
            to = (f.get("metrics") or {}).get("to_id")
            if to and any((o.get("spec") or {}).get("type") == "move_budget" for o in a.suggestions or []):
                opts = [dict(o) for o in a.suggestions]
                for o in opts:  # the receiving ad set is today's best one, not the first run's
                    if (o.get("spec") or {}).get("type") == "move_budget":
                        o["spec"] = {**o["spec"], "to": to}
                a.suggestions = opts
            a.updated_at = datetime.utcnow()
            a.times_seen += 1
        else:
            a = Alert(launch_id=launch.id, plan_id=plan_id, fingerprint=fp, severity=f["severity"],
                      kind=f["kind"], object_level=f["level"], object_id=f["object_id"],
                      object_name=f["object_name"], message=f["message"], metrics=f["metrics"],
                      suggestions=_with_target(with_specs(TEMPLATE_SUGGESTIONS.get(f["kind"], []), f["level"], f["object_id"]), f))
            session.add(a)
            new.append(a)
    # Open alerts whose condition cleared -> resolved
    for fp, a in open_alerts.items():
        if fp not in seen and a.kind not in keep_kinds:
            a.status = "resolved"
            a.updated_at = datetime.utcnow()
    session.flush()
    return new


def _with_target(options: list[dict], finding: dict) -> list[dict]:
    """move_budget needs to know where the budget goes: the best ad set named in the finding."""
    to = (finding.get("metrics") or {}).get("to_id")
    for o in options:
        if o.get("spec", {}).get("type") == "move_budget":
            if to:
                o["spec"]["to"] = to
            else:
                o.pop("spec")
    return options


def with_specs(options: list[dict], level: str, object_id: str) -> list[dict]:
    """Copy options, turning the generic 'exec' hint into a concrete 'spec' for this object."""
    out = []
    for o in options:
        spec = concretize(o.get("exec"), level, object_id)
        copy = {k: v for k, v in o.items() if k != "exec"}
        if spec:
            copy["spec"] = spec
        out.append(copy)
    return out


# ---------- Claude suggestions ----------

SUGGESTION_SCHEMA = {
    "type": "object",
    "properties": {
        "alerts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "alert_id": {"type": "integer"},
                    "diagnosis": {"type": "string"},
                    "options": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "action": {"type": "string"},
                                "expected_effect": {"type": "string"},
                                "risk": {"type": "string"},
                                "exec_type": {"type": "string", "enum": ["none", "pause", "budget_pct", "clone_winner", "refresh_creative"]},
                                "exec_pct": {"type": "number"},
                            },
                            "required": ["action", "expected_effect", "risk", "exec_type", "exec_pct"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["alert_id", "diagnosis", "options"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["alerts"],
    "additionalProperties": False,
}


def enrich_with_claude(session: Session, alert_ids: list[int]) -> None:
    """One AI call per plan, so each launch's alerts are judged against its own targets."""
    from . import llm
    if not llm.configured() or not alert_ids:
        return
    by_plan: dict[int, list[int]] = {}
    for i in alert_ids:
        a = session.get(Alert, i)
        if a:
            by_plan.setdefault(a.plan_id, []).append(i)
    for ids in by_plan.values():
        _enrich_plan_alerts(session, ids)


def _enrich_plan_alerts(session: Session, alert_ids: list[int]) -> None:
    alerts = [session.get(Alert, i) for i in alert_ids]
    plan = session.get(Plan, alerts[0].plan_id)
    content = plan.versions[-1].content
    payload = [{"alert_id": a.id, "kind": a.kind, "severity": a.severity, "object": f"{a.object_level} {a.object_name}",
                "message": a.message, "metrics": a.metrics} for a in alerts]
    prompt = (
        "You are the media buyer monitoring this Meta campaign. For each alert give a one-line diagnosis "
        "and 2-3 concrete options with the expected effect (numbers where possible) and the risk. "
        "Options must be things the user can do in Ads Manager today. If an option is exactly 'pause this "
        "object' set exec_type=pause; if it is 'change this object's daily budget by N%' set "
        "exec_type=budget_pct and exec_pct=N (negative to cut, within +/-30); for an ad, 'clone this winning ad "
        "into the other ad sets' is exec_type=clone_winner and 'make a fresh image variant of this ad' is "
        "exec_type=refresh_creative; otherwise exec_type=none, exec_pct=0. Only the alert's own object (an ad set "
        "or ad) can be executed.\n\n"
        f"PLAN TARGETS: {json.dumps(content.get('monitoring_targets'))}\n"
        f"PLAN KILL RULES: {json.dumps(content.get('kill_rules'))}\n"
        f"PLAN SCALE RULES: {json.dumps(content.get('scale_rules'))}\n"
        f"PRODUCT: {plan.product} ({plan.niche}) -> {plan.country}\n\n"
        f"ALERTS:\n{json.dumps(payload, indent=1)}"
    )
    from . import llm
    try:
        data = llm.structured("You are an experienced Meta Ads media buyer for an online store.", prompt,
                              SUGGESTION_SCHEMA, "Alert advice", effort="medium", max_tokens=8000)
    except Exception as ex:  # keep template suggestions on any failure (budget, network, bad answer)
        log.warning("AI enrichment failed: %s", ex)
        return
    by_id = {a.id: a for a in alerts}
    for item in data.get("alerts") or []:
        try:
            a = by_id.get(int(item.get("alert_id"))) if isinstance(item, dict) else None
        except (TypeError, ValueError):
            a = None
        if a and item.get("diagnosis") and item.get("options"):
            opts = []
            for o in item["options"]:
                if not isinstance(o, dict) or not o.get("action"):
                    continue
                try:
                    pct = float(o.get("exec_pct") or 0)
                except (TypeError, ValueError):
                    pct = 0.0
                spec = concretize({"type": o.get("exec_type", "none"), "pct": pct}, a.object_level, a.object_id)
                opt = {"action": o["action"], "expected_effect": o.get("expected_effect", ""), "risk": o.get("risk", "")}
                if spec:
                    opt["spec"] = spec
                opts.append(opt)
            # keep the app's own budget move: only it knows which ad set receives the money
            keep = [o for o in (a.suggestions or []) if o.get("spec", {}).get("type") == "move_budget"]
            a.suggestions = [{"diagnosis": item["diagnosis"]}] + keep + opts
    session.commit()


# ---------- run all ----------

_run_lock = threading.Lock()


def run_once() -> dict:
    if not _run_lock.acquire(blocking=False):  # timer and "Check now" never run together
        return {"skipped": "already running"}
    _state["running"] = True
    result: dict = {"checked": [], "errors": []}
    try:
        with get_session() as s:
            from . import brands
            launches = s.scalars(select(Launch).where(Launch.status.not_in(INACTIVE_LAUNCH))).all()
            if not launches:
                result["note"] = "No launched campaigns to monitor."
                return result
            # each store's campaigns are read with that store's own Meta connection
            groups: dict[int, list[Launch]] = {}
            for l in launches:
                groups.setdefault(brands.launch_brand(s, l), []).append(l)
            conns: dict[int, tuple] = {}
            frozen: set[int] = set()  # stores whose ad account Meta has restricted: no automatic changes
            for bid in groups:
                try:
                    bvals = brands.settings_for(s, bid)
                    c = MetaClient(bvals["meta_access_token"])
                    info = c.account(ad_account_path(bvals["meta_ad_account_id"]))
                    cur = info.get("currency", "USD")
                    conns[bid] = (c, cur, ad_account_path(bvals["meta_ad_account_id"]))
                    from .adpolicy import account_problem
                    if problem := account_problem(info):
                        frozen.add(bid)
                        result["errors"].append(f"{'main store' if not bid else f'brand {bid}'}: {problem}")
                except (MetaError, brands.BrandError) as ex:
                    result["errors"].append(f"{'main store' if not bid else f'brand {bid}'}: {ex}")
            if not conns:
                return result
            new_ids: list[int] = []
            try:
                result["action_outcomes"] = len(evaluate_action_outcomes(s))
            except Exception as ex:
                s.rollback()
                log.warning("action outcome evaluation failed: %s", ex)
            for l in launches:
                bid = brands.launch_brand(s, l)
                if bid not in conns:
                    continue  # its store's connection failed; reported above
                client, currency, _act = conns[bid]
                try:
                    r = check_launch(s, client, l, (l.objects or {}).get("currency") or currency)
                    result["checked"].append(r)
                    new_ids += r["new_alerts"]
                except MetaRateLimited as ex:  # Meta asked to slow down: stop calling it in this run
                    s.rollback()
                    result["errors"].append(str(ex))
                    result["rate_limited"] = True
                    break
                except Exception as ex:  # one bad launch must not stop the others
                    s.rollback()
                    log.warning("check failed for launch %s: %s", l.id, ex)
                    result["errors"].append(f"launch {l.id}: {ex}")
            result["new_alerts"] = len(new_ids)
            if new_ids:
                try:
                    enrich_with_claude(s, new_ids)
                except Exception as ex:  # keep the template advice and carry on
                    s.rollback()
                    log.warning("AI advice failed: %s", ex)
                    result["errors"].append(f"AI advice: {ex}")
            # automation also runs on quiet runs: alerts held back earlier get their turn
            try:
                from .actions import pending_alerts
                checked_ok = {r["launch_id"] for r in result["checked"]}
                logs = []
                for bid, (client, _cur, act) in conns.items():  # each store acts through its own connection
                    if bid in frozen or result.get("rate_limited") or meta_paused()[0]:
                        continue
                    mine = checked_ok & {l.id for l in groups[bid]}
                    logs += auto_apply(s, client, pending_alerts(s, mine, act))
                result["auto_actions"] = [{"alert_id": l.alert_id, "ok": bool(l.ok),
                                           "description": l.description, "error": l.error} for l in logs]
            except Exception as ex:
                s.rollback()
                log.warning("auto-apply failed: %s", ex)
                result["errors"].append(f"auto-actions: {ex}")
            if new_ids:
                try:
                    result["email"] = send_alert_email(s, new_ids)
                except Exception as ex:  # never let email failures break monitoring
                    log.warning("alert email failed: %s", ex)
                    result["errors"].append(f"email: {ex}")
            _maybe_send_digest(s, result)
        return result
    finally:
        _state["running"] = False
        _run_lock.release()
        _state["last_run"] = datetime.utcnow().isoformat()
        _state["last_result"] = result
        _record_run(result)


def _record_run(result: dict) -> None:
    """Activity lines for what the monitor did on its own (quiet runs with nothing to check are skipped)."""
    from . import activity
    checked, errors = result.get("checked", []), result.get("errors", [])
    if checked or errors:
        detail = f"{len(checked)} campaigns checked, {result.get('new_alerts', 0)} new alerts"
        activity.record("monitor", "check", "Monitor checked the campaigns", detail + (f"; {'; '.join(errors)}" if errors else ""),
                        "warning" if errors else "ok", "monitor")
    for a in result.get("auto_actions", []):
        activity.record("action", "auto", "Automatic change on Meta" if a["ok"] else "Automatic change failed",
                        a["description"] + (f": {a['error']}" if a.get("error") else ""), "ok" if a["ok"] else "error", "monitor")
    if result.get("email", {}).get("sent"):
        activity.record("email", "alerts", f"Alert email sent ({result['email']['sent']} alerts)", "", "ok", "monitor")
    if result.get("digest"):
        activity.record("email", "digest", "Weekly digest sent", "", "ok", "monitor")


def _maybe_send_digest(session: Session, result: dict) -> None:
    """Weekly digest: first run on/after DIGEST_WEEKDAY once 6+ days have passed since the last one."""
    from datetime import date

    from .config import DIGEST_WEEKDAY

    if not notify_status()["configured"]:
        return
    last = settings_store.get(session, "last_digest_at")
    today = date.today()
    if today.weekday() != DIGEST_WEEKDAY:
        return
    if last and (today - date.fromisoformat(last[:10])).days < 6:
        return
    try:
        result["digest"] = send_digest(session)
        settings_store.set_many(session, {"last_digest_at": today.isoformat()})
    except Exception as ex:
        log.warning("digest failed: %s", ex)
        result["errors"].append(f"digest: {ex}")


def start_scheduler() -> None:
    def loop():
        try:
            run_once()
        except Exception as ex:
            log.exception("monitor run failed: %s", ex)
        t = threading.Timer(max(MONITOR_INTERVAL_HOURS, 1.0) * 3600, loop)  # never more often than hourly
        t.daemon = True
        t.start()

    t = threading.Timer(30, loop)  # first run 30s after startup
    t.daemon = True
    t.start()
