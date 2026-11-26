"""Pre-launch checklist. Hard failures block the launch; warnings explain what will be skipped."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import settings_store
from .benchmarks import get_country
from . import fx
from .db import Creative, Plan
from .meta_client import MetaClient, MetaError
from .settings_store import ad_account_path


def check_plan(session: Session, plan: Plan, live: bool = True) -> dict:
    from . import brands
    try:
        vals = brands.with_landing(brands.settings_for(session, plan.brand_id), plan)
    except brands.BrandError as ex:
        return {"ready": False, "checks": [{"key": "brand", "ok": False, "hard": True, "text": str(ex)}]}
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
            from .adpolicy import account_problem
            problem = account_problem(info)
            add("meta_live", True, f"Token valid ({me.get('name', '')}); account '{info.get('name', '')}' in {currency}")
            add("account_status", not problem, problem or "Ad account is active")
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
    if live and vals["store_url"]:
        from .storecheck import check as store_check
        for c in store_check(vals["store_url"]):
            add(c["key"], c["ok"], c["text"], hard=c["hard"])
    add("pixel", bool(vals["meta_pixel_id"]), "Pixel set: ad sets optimise for Purchase" if vals["meta_pixel_id"]
        else "No pixel: ad sets will optimise for link clicks, not purchases", hard=False)

    content = plan.versions[-1].content
    n_creatives = len(content.get("creatives", []))
    selected = session.scalars(select(Creative).where(Creative.plan_id == plan.id, Creative.selected == 1)).all()
    covered = {c.creative_index for c in selected}
    url_images = [u for u in vals["creative_image_urls"].split(",") if u.strip()]
    brief = plan.brief or {}
    if brief.get("scope") == "catalogue":
        ok = bool(brief.get("product_set_id") and brief.get("catalog_id"))
        add("catalogue", ok, f"Catalogue ads from '{brief.get('product_set_name', '')}': the pictures come from your "
                             "catalogue" if ok else "The catalogue could not be read: add catalog_management to the Meta "
                             "token, then make the plan again")
    elif covered or url_images:
        missing = [i + 1 for i in range(n_creatives) if i not in covered]
        if not missing or url_images:
            add("images", True, f"{len(selected)} selected image(s)" + (f"; URL fallback for creatives {missing}" if missing else ""), hard=False)
        else:
            add("images", False, f"Creatives {missing} have no image and will be skipped", hard=False)
    else:
        add("images", False, "No ad images: generate/upload on the plan or set image URLs; ads will be skipped", hard=False)
    from .adpolicy import check_plan as policy_check
    for f in policy_check(content, plan.brief or {}, plan.niche, plan.product):
        add("policy", False, f"Meta policy, {f['where']}: {f['text']}", hard=f["level"] == "block")
    per_slot: dict[int, int] = {}
    for c in selected:
        per_slot[c.creative_index] = per_slot.get(c.creative_index, 0) + 1
    thin = [i + 1 for i, cr in enumerate(content.get("creatives", []))
            if "carousel" in str(cr.get("format", "")).lower() and 0 < per_slot.get(i, 0) < 2]
    many = [i + 1 for i, cr in enumerate(content.get("creatives", []))
            if "carousel" in str(cr.get("format", "")).lower() and per_slot.get(i, 0) > 10]
    if many:
        add("carousel_max", False, f"Carousel creative(s) {many} have more than 10 images marked Use: only the first 10 "
                                   "become cards. Unmark the extra ones", hard=False)
    if thin:
        add("carousel", False, f"Carousel creative(s) {thin} have only one image marked Use: they will run as a single "
                               "image. Add or mark at least 2", hard=False)

    try:
        cmin = get_country(plan.country)["min_daily_usd"]
    except KeyError:
        cmin = 5
    daily_total = plan.budget_pkr / fx.pkr_per_usd() / max(plan.duration_days, 1)  # launch uses today's rate
    from .budget_fit import fit_ad_sets
    sets, folded = fit_ad_sets(content.get("ad_sets", []), daily_total, cmin)
    if daily_total < cmin:
        add("budget", False, f"Daily budget ${daily_total:.2f} is below Meta's ~${cmin}/day minimum for even one ad set. "
                             "Raise the budget or shorten the duration.")
    elif folded:
        add("budget", False, f"{', '.join(folded)} would get less than Meta's ~${cmin}/day: it is merged into the "
                             f"largest ad set at launch ({len(sets)} ad set(s) run)", hard=False)
    else:
        add("budget", True, f"Daily budget ${daily_total:.2f} split across {len(sets)} ad sets")

    add("visual_prompts", all(c.get("visual_prompt") for c in content.get("creatives", [])),
        "Creatives have visual prompts for AI images" if all(c.get("visual_prompt") for c in content.get("creatives", []))
        else "Older plan without visual prompts (AI image generation uses a generic prompt)", hard=False)

    ready = all(c["ok"] for c in checks if c["hard"])
    return {"ready": ready, "checks": checks, "currency": currency}
