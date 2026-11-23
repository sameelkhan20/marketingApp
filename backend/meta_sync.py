"""Pull 2 years of Insights per country and turn them into estimator inputs.

Weighting: the most recent 90 days count double, because CPMs and CVRs drift.
Confidence comes from purchase volume (more purchases = tighter estimate).
"""

from datetime import date, timedelta

from sqlalchemy.orm import Session

from . import fx
from .db import AccountStat
from .meta_client import MetaClient, MetaError
from .settings_store import ad_account_path

PURCHASE_ACTIONS = ("omni_purchase", "purchase", "offsite_conversion.fb_pixel_purchase")


def _purchases(row: dict) -> tuple[int, float]:
    """(count, value) from an insights row. Prefers omni_purchase, avoids double counting."""
    actions = {a["action_type"]: float(a["value"]) for a in row.get("actions", [])}
    values = {a["action_type"]: float(a["value"]) for a in row.get("action_values", [])}
    for key in PURCHASE_ACTIONS:
        if key in actions:
            return int(actions[key]), values.get(key, 0.0)
    return 0, 0.0


def to_usd(amount: float, currency: str) -> float:
    if currency == "USD":
        return amount
    if currency == "PKR":
        return amount / fx.pkr_per_usd()
    # Other currencies: left as-is; the UI shows the account currency so the user knows.
    return amount


def aggregate(rows: list[dict], currency: str, today: date | None = None) -> dict[str, dict]:
    """rows -> {country: {spend_usd, impressions, clicks, purchases, revenue_usd, cpm, ctr, cvr, confidence}}"""
    today = today or date.today()
    recent_cutoff = (today - timedelta(days=90)).isoformat()
    acc: dict[str, dict] = {}

    for r in rows:
        c = (r.get("country") or "XX").upper()
        w = 2.0 if r.get("date_start", "") >= recent_cutoff else 1.0
        spend = to_usd(float(r.get("spend", 0)), currency)
        imp = int(r.get("impressions", 0))
        clk = int(r.get("inline_link_clicks", 0))
        pur, rev = _purchases(r)
        a = acc.setdefault(c, {"spend_usd": 0, "impressions": 0, "clicks": 0, "purchases": 0, "revenue_usd": 0,
                               "w_spend": 0, "w_imp": 0, "w_clk": 0, "w_pur": 0})
        a["spend_usd"] += spend; a["impressions"] += imp; a["clicks"] += clk
        a["purchases"] += pur; a["revenue_usd"] += to_usd(rev, currency)
        a["w_spend"] += spend * w; a["w_imp"] += imp * w; a["w_clk"] += clk * w; a["w_pur"] += pur * w

    out: dict[str, dict] = {}
    for c, a in acc.items():
        if a["w_imp"] == 0:
            continue
        cpm = a["w_spend"] / a["w_imp"] * 1000
        ctr = a["w_clk"] / a["w_imp"]
        cvr = a["w_pur"] / a["w_clk"] if a["w_clk"] else 0.0
        p = a["purchases"]
        confidence = "high" if p >= 100 else "medium" if p >= 20 else "low"
        out[c] = {
            "spend_usd": round(a["spend_usd"], 2), "impressions": a["impressions"], "clicks": a["clicks"],
            "purchases": p, "revenue_usd": round(a["revenue_usd"], 2),
            "cpm_usd": round(cpm, 2), "ctr": round(ctr, 4), "cvr": round(cvr, 4), "confidence": confidence,
        }
    return out


def sync_account(session: Session, token: str, account_id: str, years: int = 2) -> dict:
    client = MetaClient(token)
    act = ad_account_path(account_id)
    info = client.account(act)
    currency = info.get("currency", "USD")
    if currency not in ("USD", "PKR"):
        # to_usd() cannot convert other currencies; syncing would store wrong CPMs silently.
        raise MetaError(f"Ad account currency {currency} is not supported for history sync yet (USD or PKR only).")
    until = date.today()
    since = until - timedelta(days=365 * years)

    rows = client.insights_by_country(act, since.isoformat(), until.isoformat())
    stats = aggregate(rows, currency)

    session.query(AccountStat).delete()
    for c, s in stats.items():
        session.add(AccountStat(country=c, since=since.isoformat(), until=until.isoformat(), **s))
    session.commit()

    return {"account": info, "currency": currency, "rows": len(rows), "countries": len(stats),
            "since": since.isoformat(), "until": until.isoformat()}
