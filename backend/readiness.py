"""Pre-launch checklist. Hard failures block the launch; warnings explain what will be skipped."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import settings_store
from .benchmarks import get_country
from .config import PKR_PER_USD
from .db import Creative, Plan
from .meta_client import MetaClient, MetaError
from .settings_store import ad_account_path


def check_plan(session: Session, plan: Plan, live: bool = True) -> dict:
    vals = settings_store.get_all(session)
    checks: list[dict] = []

    def add(key, ok, text, hard=True):
        checks.append({"key": key, "ok": bool(ok), "hard": hard, "text": text})

    add("approved", plan.status == "approved", "Plan is approved" if plan.status == "approved"
        else f"Plan status is '{plan.status}' (approve it first)" if plan.status != "launched" else "Plan already launched")

    token, account = vals["meta_access_token"], vals["meta_ad_account_id"]
    add("token", bool(token), "Meta access token set" if token else "Meta access token missing (Connections page)")
    add("account", bool(account), "Ad account id set" if account else "Ad account id missing (Connections page)")

    currency = None
    if live and token and account:
        try:
            c = MetaClient(token)
            me = c.me()
            info = c.account(ad_account_path(account))
            currency = info.get("currency")
            status_ok = info.get("account_status") in (1, "1", None)
            add("meta_live", True, f"Token valid ({me.get('name', '')}); account '{info.get('name', '')}' in {currency}"
                + ("" if status_ok else f", status {info.get('account_status')} (check billing)"))
            add("currency", currency in ("USD", "PKR"), f"Account currency {currency} supported" if currency in ("USD", "PKR")
                else f"Account currency {currency} not supported for budgets (USD or PKR only)")
        except MetaError as ex:
            add("meta_live", False, f"Meta rejected the token/account: {ex}")
        if vals["meta_pixel_id"]:
            from .tracking import problems
            try:
                issues = problems(MetaClient(token).pixel_health(vals["meta_pixel_id"]))
                add("tracking", not issues, "Pixel is firing" if not issues
                    else "Tracking: " + "; ".join(i["text"] for i in issues), hard=False)
            except MetaError as ex:
                add("tracking", False, f"Could not read the pixel: {ex}", hard=False)

    add("page", bool(vals["meta_page_id"]), "Facebook Page id set" if vals["meta_page_id"] else "Page id missing: ads cannot be created")
    add("store_url", bool(vals["store_url"]), "Store URL set" if vals["store_url"] else "Store URL missing: ads cannot be created")
    add("pixel", bool(vals["meta_pixel_id"]), "Pixel set: ad sets optimise for Purchase" if vals["meta_pixel_id"]
        else "No pixel: ad sets will optimise for link clicks, not purchases", hard=False)

    content = plan.versions[-1].content
    n_creatives = len(content.get("creatives", []))
    selected = session.scalars(select(Creative).where(Creative.plan_id == plan.id, Creative.selected == 1)).all()
    covered = {c.creative_index for c in selected}
    url_images = [u for u in vals["creative_image_urls"].split(",") if u.strip()]
    if covered or url_images:
        missing = [i + 1 for i in range(n_creatives) if i not in covered]
        if not missing or url_images:
            add("images", True, f"{len(selected)} selected image(s)" + (f"; URL fallback for creatives {missing}" if missing else ""), hard=False)
        else:
            add("images", False, f"Creatives {missing} have no image and will be skipped", hard=False)
    else:
        add("images", False, "No ad images: generate/upload on the plan or set image URLs; ads will be skipped", hard=False)

    try:
        cmin = get_country(plan.country)["min_daily_usd"]
    except KeyError:
        cmin = 5
    daily_total = plan.budget_pkr / PKR_PER_USD / max(plan.duration_days, 1)
    low = [a["name"] for a in content.get("ad_sets", []) if daily_total * float(a.get("daily_budget_pct", 0)) / 100 < cmin]
    add("budget", not low, f"Daily budget ${daily_total:.2f} split across {len(content.get('ad_sets', []))} ad sets"
        if not low else f"Ad sets below Meta's ~${cmin}/day minimum: {', '.join(low)}. Raise the budget, shorten the duration or merge ad sets.")

    add("visual_prompts", all(c.get("visual_prompt") for c in content.get("creatives", [])),
        "Creatives have visual prompts for AI images" if all(c.get("visual_prompt") for c in content.get("creatives", []))
        else "Older plan without visual prompts (AI image generation uses a generic prompt)", hard=False)

    ready = all(c["ok"] for c in checks if c["hard"])
    return {"ready": ready, "checks": checks, "currency": currency}
