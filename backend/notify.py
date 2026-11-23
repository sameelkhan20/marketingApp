"""Email alerts, same delivery as the Social Agent: the Gmail API first (Google sign-in, no password,
lands in the inbox), Brevo's API when Gmail is not set up or refuses. One email per monitor run that
produced new alerts, the weekly digest, and a test email from the UI.
"""

import base64
import html
import logging
import re
import time
from email.message import EmailMessage

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import (ALERT_EMAIL, ALERT_FROM, ALERT_MIN_SEVERITY, APP_BASE_URL, BREVO_API_KEY, GMAIL_CLIENT_ID,
                     GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN, GMAIL_SENDER)
from .db import Alert, Plan

log = logging.getLogger("notify")

SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}
COLORS = {"info": "#2fb37a", "warning": "#e0a640", "critical": "#e25c5c"}
SENDER_NAME = "Ads Planner"

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
BREVO_URL = "https://api.brevo.com/v3/smtp/email"
_gmail_token: dict = {"value": "", "until": 0.0}  # short-lived access token, reused until it expires


def recipients() -> list[str]:
    """ALERT_EMAIL may hold several addresses, separated by commas or semicolons."""
    return [a.strip() for a in re.split(r"[,;]", ALERT_EMAIL or "") if "@" in a]


def _gmail_creds() -> tuple[str, str]:
    """(refresh token, sender): the in-app Connect sign-in first, else the .env values."""
    from . import secrets_store
    rt = secrets_store.get("gmail_refresh_token")
    if rt:
        return rt, secrets_store.get("gmail_sender")
    return GMAIL_REFRESH_TOKEN, GMAIL_SENDER


def gmail_configured() -> bool:
    rt, sender = _gmail_creds()
    return bool(recipients() and GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET and rt and sender)


def brevo_configured() -> bool:
    return bool(recipients() and BREVO_API_KEY and ALERT_FROM)


def status() -> dict:
    """What the UI shows: is email configured, how, and to whom."""
    channels = [c for c, ok in (("gmail", gmail_configured()), ("brevo", brevo_configured())) if ok]
    from . import gmail_auth, secrets_store
    return {"gmail_in_app": bool(secrets_store.get("gmail_refresh_token")),"provider": " + ".join(channels) or "off", "to": ", ".join(recipients()), "configured": bool(channels),
            "min_severity": ALERT_MIN_SEVERITY, "gmail_sender": _gmail_creds()[1],
            "gmail_can_connect": gmail_auth.client_configured(), "has_recipients": bool(recipients())}


# ---------- transport ----------

def _gmail_access_token() -> str:
    if _gmail_token["value"] and time.monotonic() < _gmail_token["until"]:
        return _gmail_token["value"]
    r = requests.post(GOOGLE_TOKEN_URL, data={"client_id": GMAIL_CLIENT_ID, "client_secret": GMAIL_CLIENT_SECRET,
                                              "refresh_token": _gmail_creds()[0], "grant_type": "refresh_token"},
                      timeout=20)
    if r.status_code >= 400:
        raise RuntimeError(f"Google login refused ({r.status_code}): {r.text[:200]}")
    data = r.json()
    _gmail_token["value"] = data["access_token"]
    _gmail_token["until"] = time.monotonic() + max(60, int(data.get("expires_in", 3600)) - 120)
    return _gmail_token["value"]


def _send_gmail(subject: str, html_body: str, text_body: str) -> str:
    """Returns "" on success, else the reason. Never raises."""
    try:
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = subject, f"{SENDER_NAME} <{_gmail_creds()[1]}>", ", ".join(recipients())
        msg.set_content(text_body or " ")
        msg.add_alternative(html_body, subtype="html")
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        r = requests.post(GMAIL_SEND_URL, json={"raw": raw},
                          headers={"Authorization": f"Bearer {_gmail_access_token()}"}, timeout=20)
        if r.status_code >= 400:
            if r.status_code == 401:
                _gmail_token["value"] = ""  # a fresh token next time
            return f"Gmail {r.status_code}: {r.text[:300]}"
        return ""
    except (requests.RequestException, RuntimeError, KeyError, ValueError) as ex:
        return f"Gmail: {str(ex)[:300]}"


def _send_brevo(subject: str, html_body: str, text_body: str) -> str:
    try:
        r = requests.post(BREVO_URL, headers={"api-key": BREVO_API_KEY, "accept": "application/json"},
                          json={"sender": {"name": SENDER_NAME, "email": ALERT_FROM},
                                "to": [{"email": a} for a in recipients()], "subject": subject,
                                "textContent": text_body or " ", "htmlContent": html_body}, timeout=20)
        if r.status_code >= 400:
            return f"Brevo {r.status_code}: {r.text[:300]}"
        return ""
    except requests.RequestException as ex:
        return f"Brevo: {ex}"


def send_email(subject: str, html_body: str, text_body: str) -> str:
    """Gmail first, Brevo when Gmail is not set up or refuses. Returns the channel used; raises when
    every configured channel failed, with each reason."""
    if not status()["configured"]:
        raise RuntimeError("Email is not configured. Set ALERT_EMAIL and either the GMAIL_* values "
                           "(Connect Gmail on the Connections page) or BREVO_API_KEY + ALERT_FROM in .env.")
    errors = []
    if gmail_configured():
        err = _send_gmail(subject, html_body, text_body)
        if not err:
            return "gmail"
        log.warning("Email send failed (%s)", err)
        errors.append(err)
    if brevo_configured():
        err = _send_brevo(subject, html_body, text_body)
        if not err:
            return "brevo"
        log.warning("Email send failed (%s)", err)
        errors.append(err)
    raise RuntimeError("; ".join(errors))


# ---------- rendering ----------

def render_alert_email(alerts: list[Alert], plans: dict[int, Plan]) -> tuple[str, str, str]:
    worst = max((SEVERITY_RANK[a.severity] for a in alerts), default=0)
    worst_name = next(k for k, v in SEVERITY_RANK.items() if v == worst)
    n = len(alerts)
    subject = f"[Ads Planner] {n} new alert{'s' if n != 1 else ''} ({worst_name})"
    link = f"{APP_BASE_URL}/#monitor"
    e = html.escape

    blocks, lines = [], []
    for a in alerts:
        p = plans.get(a.plan_id)
        title = f"{p.product} → {p.country}" if p else f"plan #{a.plan_id}"
        diag = next((s["diagnosis"] for s in a.suggestions if s.get("diagnosis")), "")
        opts = [s for s in a.suggestions if s.get("action")]
        m = a.metrics or {}
        stats = " · ".join(f"{k.replace('_usd', '')} {m[k]}" for k in
                           ("spend_usd", "impressions", "purchases", "ctr", "cpa_usd", "frequency", "roas") if k in m)

        blocks.append(f"""
<div style="border-left:4px solid {COLORS[a.severity]};padding:10px 14px;margin:0 0 14px;background:#f7f8fa;border-radius:6px">
  <div style="font-size:12px;color:#666;text-transform:uppercase">{e(a.severity)} · {e(title)} · {e(a.object_level)} {e(a.object_name)}</div>
  <div style="font-weight:600;margin:4px 0">{e(a.message)}</div>
  <div style="font-size:12px;color:#666">{e(stats)}</div>
  {f'<div style="font-style:italic;margin-top:6px">{e(diag)}</div>' if diag else ''}
  {'<ol style="margin:6px 0 0;padding-left:20px;font-size:13px">' + ''.join(
      f'<li style="margin-bottom:4px">{e(o["action"])} <span style="color:#2fb37a">→ {e(o["expected_effect"])}</span> '
      f'<span style="color:#888">(risk: {e(o["risk"])})</span></li>' for o in opts) + '</ol>' if opts else ''}
</div>""")
        lines.append(f"[{a.severity.upper()}] {title} / {a.object_level} {a.object_name}\n  {a.message}\n  {stats}")
        if diag:
            lines.append(f"  {diag}")
        for i, o in enumerate(opts, 1):
            lines.append(f"  {i}. {o['action']} -> {o['expected_effect']} (risk: {o['risk']})")
        lines.append("")

    html_body = f"""<!doctype html><html><body style="font-family:system-ui,Segoe UI,Roboto,sans-serif;color:#222;max-width:680px;margin:0 auto;padding:20px">
<h2 style="margin:0 0 4px">{n} new alert{'s' if n != 1 else ''} on your Meta campaigns</h2>
<p style="color:#666;margin:0 0 18px">The monitor found these deviations from your plan. Nothing has been changed automatically.</p>
{''.join(blocks)}
<p><a href="{e(link)}" style="display:inline-block;background:#4f7cff;color:#fff;padding:10px 16px;border-radius:6px;text-decoration:none">Open dashboard</a></p>
<p style="color:#999;font-size:12px">Alerts repeat only when they change. Acknowledge or resolve them in the dashboard.</p>
</body></html>"""
    text_body = f"{n} new alert(s) on your Meta campaigns\n\n" + "\n".join(lines) + f"\nDashboard: {link}\n"
    return subject, html_body, text_body


# ---------- entry points ----------

def send_alert_email(session: Session, alert_ids: list[int]) -> dict:
    if not status()["configured"]:
        return {"sent": 0, "reason": "email not configured"}
    min_rank = SEVERITY_RANK.get(ALERT_MIN_SEVERITY, 1)
    alerts = [a for a in session.scalars(select(Alert).where(Alert.id.in_(alert_ids), Alert.notified == 0))
              if SEVERITY_RANK[a.severity] >= min_rank]
    if not alerts:
        return {"sent": 0, "reason": "no alerts above minimum severity"}
    plans = {p.id: p for p in session.scalars(select(Plan).where(Plan.id.in_({a.plan_id for a in alerts})))}
    subject, html_body, text_body = render_alert_email(alerts, plans)
    send_email(subject, html_body, text_body)
    for a in alerts:
        a.notified = 1
    session.commit()
    log.info("alert email sent: %s", subject)
    return {"sent": len(alerts)}


def render_digest_email(data: dict) -> tuple[str, str, str]:
    """Weekly summary: every launch vs plan, actions taken, open alerts."""
    e = html.escape
    link = f"{APP_BASE_URL}/#monitor"
    subject = f"[Ads Planner] Weekly digest: {len(data['launches'])} campaign(s), {data['alerts_open']} open alert(s)"
    verdict_color = {"ahead": COLORS["info"], "behind": COLORS["critical"]}
    rows = "".join(
        f"<tr><td style='padding:6px 8px'>{e(l['product'])} → {e(l['country'])}</td>"
        f"<td style='padding:6px 8px;text-align:right'>${l['spend_usd']}</td>"
        f"<td style='padding:6px 8px;text-align:right'>{l['purchases']} / {l['predicted_so_far']}</td>"
        f"<td style='padding:6px 8px'><span style='color:{verdict_color.get(l['verdict'], COLORS['warning'])}'>{e(l['verdict'].replace('_', ' '))}</span></td>"
        f"<td style='padding:6px 8px'>{l['days_active']}/{l['duration_days']} d · {l['open_alerts']} alerts</td></tr>"
        for l in data["launches"]) or "<tr><td style='padding:6px 8px;color:#888' colspan='5'>No launched campaigns</td></tr>"
    acts = "".join(f"<li>{e(a['description'])} <span style='color:#888'>({e(a['mode'])}{', ' + e(a['outcome']) if a.get('outcome') else ''})</span></li>"
                   for a in data["actions"]) or "<li style='color:#888'>No actions this week</li>"
    html_body = f"""<!doctype html><html><body style="font-family:system-ui,Segoe UI,Roboto,sans-serif;color:#222;max-width:680px;margin:0 auto;padding:20px">
<h2 style="margin:0 0 12px">Weekly digest</h2>
<table style="border-collapse:collapse;width:100%;font-size:14px"><tr style="background:#f0f1f4"><th style="text-align:left;padding:6px 8px">Campaign</th><th style="padding:6px 8px">Spend</th><th style="padding:6px 8px">Purchases / expected</th><th style="text-align:left;padding:6px 8px">Verdict</th><th style="text-align:left;padding:6px 8px">Progress</th></tr>{rows}</table>
<h3 style="margin:18px 0 6px">Actions this week</h3><ul style="margin:0;padding-left:20px;font-size:14px">{acts}</ul>
<p style="margin-top:18px"><a href="{e(link)}" style="display:inline-block;background:#4f7cff;color:#fff;padding:10px 16px;border-radius:6px;text-decoration:none">Open dashboard</a></p>
</body></html>"""
    text = "Weekly digest\n\n" + "\n".join(
        f"- {l['product']} -> {l['country']}: ${l['spend_usd']} spent, {l['purchases']} purchases vs {l['predicted_so_far']} expected "
        f"({l['verdict']}), day {l['days_active']}/{l['duration_days']}, {l['open_alerts']} open alerts" for l in data["launches"]
    ) + "\n\nActions:\n" + "\n".join(f"- {a['description']} ({a['mode']}{', ' + a['outcome'] if a.get('outcome') else ''})" for a in data["actions"]) \
        + f"\n\nDashboard: {link}\n"
    return subject, html_body, text


def digest_data(session: Session) -> dict:
    from datetime import datetime, timedelta

    from sqlalchemy import func

    from .db import ActionLog, ActionOutcome, Launch, PlanOutcome

    week_ago = datetime.utcnow() - timedelta(days=7)
    launches = []
    for l in session.scalars(select(Launch).where(Launch.status.not_in(("archived", "completed")))):
        po = session.get(PlanOutcome, l.id)
        p = session.get(Plan, l.plan_id)
        n_open = session.scalar(select(func.count()).select_from(Alert).where(Alert.launch_id == l.id, Alert.status == "open")) or 0
        launches.append({"product": p.product, "country": p.country, "spend_usd": po.spend_usd if po else 0,
                         "purchases": po.purchases if po else 0, "predicted_so_far": po.predicted_so_far if po else 0,
                         "verdict": po.verdict if po else "no_data", "days_active": po.days_active if po else 0,
                         "duration_days": p.duration_days, "open_alerts": n_open})
    ocs = {o.action_id: o.verdict for o in session.scalars(select(ActionOutcome))}
    actions = [{"description": a.description, "mode": a.mode, "outcome": ocs.get(a.id)}
               for a in session.scalars(select(ActionLog).where(ActionLog.created_at >= week_ago, ActionLog.ok == 1)
                                        .order_by(ActionLog.id.desc()))]
    alerts_open = session.scalar(select(func.count()).select_from(Alert).where(Alert.status == "open")) or 0
    return {"launches": launches, "actions": actions, "alerts_open": alerts_open}


def send_digest(session: Session) -> dict:
    data = digest_data(session)
    subject, html_body, text = render_digest_email(data)
    send_email(subject, html_body, text)
    return {"sent": True, "launches": len(data["launches"]), "actions": len(data["actions"])}


def send_test_email() -> str:
    return send_email(
        "[Ads Planner] Test email",
        "<p>Email alerts are working. You will get a message whenever the monitor finds new alerts "
        f"at or above <b>{html.escape(ALERT_MIN_SEVERITY)}</b> severity.</p>",
        f"Email alerts are working. Minimum severity: {ALERT_MIN_SEVERITY}.",
    )
