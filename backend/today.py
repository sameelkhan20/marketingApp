"""Today: the few things worth doing now, ranked. Built from the app's own data, no AI call."""

from datetime import date, timedelta

from sqlalchemy import func, select

from . import settings_store, usage
from . import llm
from . import fx
from .db import AccountStat, Alert, Launch, Plan, PlanOutcome, get_session
from .seasonality import events

MARKETS = ["US", "GB", "CA", "AU"]
PRIORITY = {"high": 0, "medium": 1, "low": 2}


def _item(priority, title, text, view, label, plan_id=None):
    return {"priority": priority, "title": title, "text": text, "view": view, "label": label, "plan_id": plan_id}


def build() -> dict:
    items: list[dict] = []
    today = date.today()
    with get_session() as s:
        vals = settings_store.get_all(s)
        plans = s.scalars(select(Plan).order_by(Plan.id.desc()).limit(200)).all()
        launches = s.scalars(select(Launch)).all()
        outcomes = {po.launch_id: po for po in s.scalars(select(PlanOutcome))}
        open_alerts = s.scalars(select(Alert).where(Alert.status == "open")).all()
        last_sync = s.scalar(select(func.max(AccountStat.synced_at)))

        # --- sales calendar: the next 21 days in the markets this store sells to
        markets = list(dict.fromkeys([p.country for p in plans[:20]] + MARKETS))[:6]
        seen = set()
        for c in markets:
            for e in events(c, today, today + timedelta(days=21)):
                if e["name"] in seen:
                    continue
                seen.add(e["name"])
                days = (date.fromisoformat(e["date"]) - today).days
                when = "today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days"
                items.append(_item("high" if days <= 7 else "medium", f"{e['name']} {when}",
                                   f"{e['note']}. Plan a campaign for it now so it is through review and learning before the day.",
                                   "plan", "Start a plan"))

        # --- plans waiting on the owner
        drafts = [p for p in plans if p.status == "draft" and (today - p.created_at.date()).days <= 14]
        for p in drafts[:3]:  # the newest three; more would bury everything else
            items.append(_item("medium", f"Plan #{p.id} waits for approval",
                               f"{p.product} to {p.country}, {p.estimate.get('orders_low')}–{p.estimate.get('orders_high')} orders expected.",
                               "plan", "Review", p.id))
        if len(drafts) > 3:
            items.append(_item("low", f"{len(drafts) - 3} more plans wait for approval",
                               "Approve the ones you want and delete the rest from My plans.", "history", "Open My plans"))
        for p in plans:
            if p.status == "approved":
                items.append(_item("medium", f"Plan #{p.id} is approved but not launched",
                                   f"{p.product} to {p.country}. Check the readiness list and launch it.", "plan", "Open", p.id))

        # --- campaigns
        waiting = []
        for l in launches:
            if l.status in ("completed", "archived", "deleted"):
                continue
            p = s.get(Plan, l.plan_id)
            po = outcomes.get(l.id)
            if l.status == "paused" and not (l.objects or {}).get("activated_at"):
                waiting.append(p)
            elif po and po.verdict == "behind" and po.days_active >= 3:
                items.append(_item("high", f"{p.product} to {p.country} is behind plan",
                                   f"{po.purchases} orders against {po.predicted_so_far} expected by day {po.days_active}. "
                                   "Look at its alerts: move budget away from weak ad sets or refresh creatives.",
                                   "monitor", "Open campaign"))
            elif po and po.verdict == "ahead" and po.days_active >= 3:
                items.append(_item("medium", f"{p.product} to {p.country} is ahead of plan",
                                   f"{po.purchases} orders against {po.predicted_so_far} expected. A good moment to scale the best ad set by 20%.",
                                   "monitor", "Open campaign"))

        for p in waiting[:2]:
            items.append(_item("high", f"Campaign for plan #{p.id} is still paused",
                               f"{p.product} to {p.country} was created paused. Check it in Ads Manager and switch it on.",
                               "monitor", "Open campaign"))
        if len(waiting) > 2:
            items.append(_item("medium", f"{len(waiting) - 2} more campaigns are still paused",
                               "Switch on the ones you want in Ads Manager.", "monitor", "Open campaigns"))

        # --- alerts
        crit = [a for a in open_alerts if a.severity == "critical"]
        tracking = [a for a in open_alerts if a.kind == "tracking"]
        if tracking:
            items.append(_item("high", "Tracking problem", tracking[0].message + ". Meta cannot optimise for sales until it is fixed.",
                               "monitor", "See the alert"))
        if crit:
            items.append(_item("high", f"{len(crit)} critical alert{'s' if len(crit) > 1 else ''}",
                               crit[0].message, "monitor", "See alerts"))
        elif open_alerts:
            items.append(_item("low", f"{len(open_alerts)} open alert{'s' if len(open_alerts) > 1 else ''}",
                               "Suggestions are ready; most can be applied with one click.", "monitor", "See alerts"))

        # --- setup gaps
        if not vals["meta_access_token"] or not vals["meta_ad_account_id"]:
            items.append(_item("high", "Connect the Meta ad account", "Without it nothing can be launched or measured.",
                               "meta", "Connect"))
        elif not last_sync:
            items.append(_item("medium", "Sync your Meta history",
                               "Two years of your own results make the order estimates far more accurate than benchmarks.",
                               "meta", "Sync"))
        if vals["meta_access_token"] and not vals["meta_pixel_id"]:
            items.append(_item("high", "Add the pixel id",
                               "Without it ads optimise for clicks, not purchases, and retargeting is off.", "meta", "Add pixel"))
        if not llm.configured():
            items.append(_item("medium", "Plans use the fixed template",
                               "Add GEMINI_API_KEY (or ANTHROPIC_API_KEY) on the server for plans written for each product, country and season.",
                               "billing", "See payments"))
        if not vals.get("product_photo"):
            items.append(_item("low", "Upload a product photo",
                               "Open any plan: the photo box is under Ad images. Generated images then show your real jacket.",
                               "history", "Open My plans"))

    spent, budget = usage.month_cost(), usage.budget()
    if budget and spent >= budget * 0.8:
        items.append(_item("high" if spent >= budget else "medium", "AI budget almost used",
                           f"${spent:.2f} of ${budget:.2f} this month. When it is used up, new AI work pauses.",
                           "billing", "Adjust budget"))

    items.sort(key=lambda i: PRIORITY[i["priority"]])
    running = [l for l in launches if l.status not in ("completed", "archived", "deleted")]
    orders = sum(outcomes[l.id].purchases for l in running if l.id in outcomes)
    spend = sum(outcomes[l.id].spend_usd for l in running if l.id in outcomes)
    return {
        "date": today.isoformat(),
        "items": items[:12],
        "stats": {"campaigns": len(running), "orders": orders, "spend_usd": round(spend, 2),
                  "spend_pkr": round(spend * fx.pkr_per_usd()), "cpa_usd": round(spend / orders, 2) if orders else None,
                  "open_alerts": len(open_alerts)},
    }
