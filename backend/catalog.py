"""The store's Meta catalogue: the product sets its catalogue ads used, and what each one sold.

Which ad set promoted which product set, and that ad set's results, are readable with the usual ads
permissions. A product set's name, size and catalogue need catalog_management on the token; without it the
sets are still ranked by their results, but a catalogue campaign cannot be created.
"""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy.orm import Session


def product_sets(vals: dict, years: int = 2) -> dict:
    """{"sets": [{id, name, catalog_id, product_count, campaigns, spend_usd, purchases, revenue_usd, clicks}],
    "readable": bool, "note": str}, best first."""
    from .meta_client import MetaClient, MetaError
    from .meta_sync import _purchases, to_usd
    from .settings_store import ad_account_path
    if not vals.get("meta_access_token") or not vals.get("meta_ad_account_id"):
        return {"sets": [], "readable": False, "note": "Connect the store to Meta first."}
    client = MetaClient(vals["meta_access_token"])
    act = ad_account_path(vals["meta_ad_account_id"])
    currency = client.account(act).get("currency", "USD")
    owner: dict[str, str] = {}
    sets: dict[str, dict] = {}
    # an ad set's product set: on the ad set itself, else on its ads' creatives
    on_ads: dict[str, str] = {}
    try:
        for ad in client.ads_product_sets(act):
            ps = str((ad.get("creative") or {}).get("product_set_id") or "")
            if ps and ad.get("adset_id"):
                on_ads.setdefault(str(ad["adset_id"]), ps)
    except MetaError:
        pass  # the ad set level still counts
    for a in client.adsets_promoting(act):
        ps = str((a.get("promoted_object") or {}).get("product_set_id") or "") or on_ads.get(str(a["id"]), "")
        if not ps:
            continue
        owner[str(a["id"])] = ps
        s = sets.setdefault(ps, {"id": ps, "name": "", "catalog_id": "", "product_count": 0, "campaigns": [],
                                 "spend_usd": 0.0, "purchases": 0, "revenue_usd": 0.0, "clicks": 0})
        name = (a.get("campaign") or {}).get("name") or ""
        if name and name not in s["campaigns"]:
            s["campaigns"].append(name)
    if not sets:
        return {"sets": [], "readable": False, "note": "This ad account has not run catalogue ads yet."}

    until = date.today()
    for r in client.adset_insights(act, (until - timedelta(days=365 * years)).isoformat(), until.isoformat()):
        ps = owner.get(str(r.get("adset_id")))
        if not ps:
            continue
        pur, rev = _purchases(r)
        s = sets[ps]
        s["spend_usd"] += to_usd(float(r.get("spend", 0) or 0), currency)
        s["purchases"] += pur
        s["revenue_usd"] += to_usd(rev, currency)
        s["clicks"] += int(r.get("inline_link_clicks", 0) or 0)

    readable, note = False, ""
    for s in sets.values():
        s["spend_usd"], s["revenue_usd"] = round(s["spend_usd"], 2), round(s["revenue_usd"], 2)
        try:
            d = client.product_set(s["id"])
            s["name"] = d.get("name", "")
            s["product_count"] = int(d.get("product_count") or 0)
            s["catalog_id"] = str((d.get("product_catalog") or {}).get("id") or "")
            readable = True  # one readable set shows the token can read the catalogue
        except MetaError:
            s["gone"] = True  # deleted, or not shared with this token: never chosen
        if not s["name"]:
            s["name"] = f"Product set used by {s['campaigns'][0]}" if s["campaigns"] else f"Product set {s['id']}"
    if not readable:
        note = ("The token cannot read the catalogue yet (it needs catalog_management): catalogue campaigns "
                "cannot be created until it is added.")
    ranked = sorted(sets.values(), key=lambda s: (-s["purchases"], -(s["revenue_usd"] / s["spend_usd"]
                                                                    if s["spend_usd"] else 0), -s["clicks"]))
    return {"sets": ranked, "readable": readable, "note": note}
