"""Turn an approved plan into Meta objects. Everything is created PAUSED.

Mapping:
  plan                 -> 1 campaign (OUTCOME_SALES)
  plan.ad_sets[i]      -> 1 ad set each, daily budget = total/day * daily_budget_pct
  plan.creatives[j]    -> 1 creative + 1 ad per ad set (image from settings, rotated)

Purchase optimisation needs a pixel; without one we fall back to LINK_CLICKS
and say so. Ads are skipped (not failed) when no page or image is configured.
"""

import re
import time

from . import fx
from .meta_client import MetaClient, MetaError, budget_minor_units
from .settings_store import ad_account_path

CTA_MAP = {"shop now": "SHOP_NOW", "learn more": "LEARN_MORE", "buy now": "BUY_NOW",
           "order now": "ORDER_NOW", "get offer": "GET_OFFER", "sign up": "SIGN_UP"}


def _age_range(audience: str) -> tuple[int, int]:
    m = re.search(r"(\d{2})\s*[-–]\s*(\d{2})", audience)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        if 18 <= lo < hi <= 65:
            return lo, hi
    return 18, 65


def is_retargeting(ad_set: dict) -> bool:
    return "retarget" in (ad_set.get("name", "") + " " + ad_set.get("audience", "")).lower()


def _targeting(country: str, ad_set: dict, audiences: dict | None = None) -> dict:
    lo, hi = _age_range(ad_set.get("audience", ""))
    t: dict = {
        "geo_locations": {"countries": [country]},
        "age_min": lo,
        "age_max": hi,
    }
    aud = ad_set.get("audience", "").lower()
    broad = "broad" in aud or "advantage" in aud or ad_set.get("name", "").lower().startswith("broad")
    audiences = audiences or {}
    if is_retargeting(ad_set) and audiences.get("visitors"):
        # warm people only: store visitors of the last 30 days; strict, no audience expansion
        t["custom_audiences"] = [{"id": audiences["visitors"]}]
        broad = False
    if audiences.get("buyers"):
        # never pay to reach people who bought in the last 180 days
        t["excluded_custom_audiences"] = [{"id": audiences["buyers"]}]
    # Meta requires the flag to be explicit on new ad sets; 0 keeps interest targeting strict.
    t["targeting_automation"] = {"advantage_audience": 1 if broad else 0}
    if broad:
        # With Advantage+ audience the age range is only a suggestion; Meta refuses a hard
        # age_max below 65 or age_min above 25, which would fail the whole launch.
        t["age_max"] = 65
        t["age_min"] = min(t["age_min"], 25)
    return t


VISITORS_DAYS, BUYERS_DAYS = 30, 180


def ensure_audiences(client, act: str, pixel: str, cached: dict) -> tuple[dict, str]:
    """Website custom audiences on the pixel: visitors (30 days) and buyers (180 days).
    Reuses ids created earlier for the same pixel. Returns (ids, note)."""
    if not pixel:
        return {}, "No pixel: retargeting and buyer exclusion are off."
    have = dict(cached) if cached.get("pixel") == pixel else {}
    for k in ("visitors", "buyers"):  # still there? (it may have been deleted in Ads Manager)
        if have.get(k) and hasattr(client, "get"):
            try:
                client.get(have[k], fields="id")
            except MetaError as ex:
                msg = str(ex).lower()
                if "does not exist" in msg or "error (100)" in msg or "unsupported get request" in msg:
                    have[k] = ""  # deleted in Ads Manager: make a new one
                # anything else (network, rate limit): keep the id and try it
    if have.get("visitors") and have.get("buyers"):
        return {"pixel": pixel, "visitors": have["visitors"], "buyers": have["buyers"]}, ""
    def rule(days: int, flt: dict) -> dict:
        return {"inclusions": {"operator": "or", "rules": [{
            "event_sources": [{"id": pixel, "type": "pixel"}], "retention_seconds": days * 86400,
            "filter": {"operator": "and", "filters": [flt]}}]}}
    out = {"pixel": pixel, "visitors": have.get("visitors", ""), "buyers": have.get("buyers", "")}
    try:
        if not out["visitors"]:
            out["visitors"] = client.create_custom_audience(
                act, f"Ads Planner: store visitors {VISITORS_DAYS}d",
                rule(VISITORS_DAYS, {"field": "url", "operator": "i_contains", "value": ""}),
                "Everyone who visited the store in the last 30 days (retargeting)")
        if not out["buyers"]:
            out["buyers"] = client.create_custom_audience(
                act, f"Ads Planner: buyers {BUYERS_DAYS}d",
                rule(BUYERS_DAYS, {"field": "event", "operator": "eq", "value": "Purchase"}),
                "Bought in the last 180 days (excluded from prospecting)")
    except MetaError as ex:
        # keep any audience that was made, so the next launch reuses it instead of making another
        return ({k: v for k, v in out.items() if v} if out["visitors"] or out["buyers"] else {}), (
            f"Could not create the retargeting audiences ({ex}). Accept the Custom Audience terms in "
            "Business Settings, then launch again; this launch runs without retargeting.")
    return out,         "Created the retargeting audiences (store visitors 30 days, buyers 180 days)."


def launch_plan(plan_inputs: dict, plan_content: dict, settings: dict, plan_id: int,
                creative_images: list[dict] | None = None, save_audiences=None) -> dict:
    """creative_images: [{creative_index, image_hash}] from the plan's selected app creatives.
    When present they are used instead of creative_image_urls; a creative with no image falls
    back to the URL list, and if nothing is available the ad is skipped."""
    token = settings["meta_access_token"]
    account_id = settings["meta_ad_account_id"]
    if not account_id:
        raise MetaError("Ad account id is not set on the Connections page.")

    client = MetaClient(token)
    act = ad_account_path(account_id)
    currency = client.account(act).get("currency", "USD")

    # Budget: plan is in PKR; convert to the account currency.
    total_pkr = plan_inputs["budget_pkr"]
    days = max(plan_inputs["duration_days"], 1)
    PKR_PER_USD = fx.pkr_per_usd()  # today's rate: the budget the owner set is in rupees
    if currency == "PKR":
        daily_total = total_pkr / days
    elif currency == "USD":
        daily_total = total_pkr / PKR_PER_USD / days
    else:
        raise MetaError(f"Ad account currency {currency} is not supported yet (USD or PKR only).")

    country = plan_inputs["country"].upper()
    product = plan_inputs["product"]
    pixel = settings.get("meta_pixel_id", "")
    page = settings.get("meta_page_id", "")
    link = settings.get("store_url", "")
    images = [u.strip() for u in settings.get("creative_image_urls", "").split(",") if u.strip()]

    plan_adsets = list(plan_content.get("ad_sets", []))
    notes: list[str] = []
    cached = {k: settings.get(f"aud_{k}", "") for k in ("pixel", "visitors", "buyers")}
    audiences, aud_note = ensure_audiences(client, act, settings.get("meta_pixel_id", ""), cached)
    if audiences and save_audiences:
        save_audiences(audiences)  # saved now: a later failure must not lose audiences that exist on Meta
    if aud_note:
        notes.append(aud_note)
    if not audiences.get("visitors"):
        dropped = [a.get("name") for a in plan_adsets if is_retargeting(a)]
        plan_adsets = [a for a in plan_adsets if not is_retargeting(a)]
        if dropped:
            notes.append(f"Skipped ad set {', '.join(dropped)}: no retargeting audience; its budget went to the others.")
    if not plan_adsets:
        raise MetaError("The plan has no ad sets.")
    # Shares written by the model may not add up to 100; normalise so the daily total matches the plan.
    shares = [max(float(a.get("daily_budget_pct", 0) or 0), 0.0) for a in plan_adsets]
    share_total = sum(shares) or float(len(shares))
    shares = [s / share_total if sum(shares) else 1 / len(shares) for s in shares]

    # Ad sets end when the plan ends; without end_time a daily budget would keep spending.
    end_time = int(time.time()) + days * 86400

    campaign_id = client.create_campaign(act, f"[Planner #{plan_id}] {product} - {country}",
                                         is_adset_budget_sharing_enabled=False)

    adsets: list[dict] = []
    try:
        for i, a in enumerate(plan_adsets):
            daily = daily_total * shares[i]
            fields = {
                "name": a.get("name", f"Ad set {i + 1}"),
                "campaign_id": campaign_id,
                "daily_budget": budget_minor_units(daily, currency),
                "billing_event": "IMPRESSIONS",
                "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
                "targeting": _targeting(country, a, audiences),
                "end_time": end_time,
            }
            if pixel:
                fields["optimization_goal"] = "OFFSITE_CONVERSIONS"
                fields["promoted_object"] = {"pixel_id": pixel, "custom_event_type": "PURCHASE"}
            else:
                fields["optimization_goal"] = "LINK_CLICKS"
                if i == 0:
                    notes.append("No pixel configured: ad sets optimise for LINK_CLICKS instead of PURCHASE.")
            adset_id = client.create_adset(act, **fields)
            adsets.append({"id": adset_id, "name": fields["name"], "daily_budget": round(daily, 2), "currency": currency})
    except MetaError as ex:
        # Do not leave a half-built campaign behind; the user will retry the launch.
        try:
            client.update_object(campaign_id, status="DELETED")
            cleanup = "the partial campaign was deleted"
        except MetaError:
            cleanup = f"delete campaign {campaign_id} manually in Ads Manager"
        raise MetaError(f"Ad set {len(adsets) + 1} failed ({ex}); {cleanup}.")

    hashes: dict[int, list[str]] = {}
    for ci in creative_images or []:
        hashes.setdefault(int(ci["creative_index"]), []).append(ci["image_hash"])

    ads: list[dict] = []
    if not page or not link:
        notes.append("No page id or store URL configured: campaign and ad sets created, ads skipped.")
    elif not images and not hashes:
        notes.append("No creatives: generate/upload images on the plan or set image URLs on the Connections page. Ads skipped.")
    else:
        for adset in adsets:
            for j, cr in enumerate(plan_content.get("creatives", [])):
                cta = CTA_MAP.get(cr.get("cta", "").lower(), "SHOP_NOW")
                link_data = {
                    "link": link,
                    "message": cr.get("primary_text", ""),
                    "name": cr.get("headline", product),
                    "call_to_action": {"type": cta, "value": {"link": link}},
                }
                if hashes.get(j):
                    link_data["image_hash"] = hashes[j][0]
                elif images:
                    link_data["picture"] = images[j % len(images)]
                else:
                    notes.append(f"Creative {j + 1} has no image; skipped.")
                    continue
                try:
                    creative_id = client.create_creative(
                        act,
                        name=f"{adset['name']} / creative {j + 1}",
                        object_story_spec={"page_id": page, "link_data": link_data},
                    )
                    ad_id = client.create_ad(act, f"{adset['name']} - C{j + 1}", adset["id"], creative_id)
                    ads.append({"id": ad_id, "adset_id": adset["id"], "creative_id": creative_id,
                                "creative_index": j, "name": f"{adset['name']} - C{j + 1}"})
                except MetaError as ex:
                    notes.append(f"Ad {j + 1} in '{adset['name']}' failed: {ex}")

    if hashes:
        notes.append(f"Used {sum(len(v) for v in hashes.values())} app creative image(s) uploaded to Meta.")

    return {
        "campaign_id": campaign_id,
        "account": act,
        "currency": currency,
        "pkr_per_usd": PKR_PER_USD,  # the rate this campaign was built with (guardrails keep using it)
        "adsets": adsets,
        "ads": ads,
        "notes": notes,
        "audiences": audiences,
        "ads_manager_url": f"https://adsmanager.facebook.com/adsmanager/manage/campaigns?act={act[4:]}&selected_campaign_ids={campaign_id}",
    }
