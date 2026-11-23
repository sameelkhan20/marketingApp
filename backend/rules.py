"""Deterministic rules engine: plan targets vs live metrics -> alerts.

Pure functions, no network, so they can be unit-tested with fake rows.
Each finding: {kind, severity, level, object_id, object_name, message, metrics}.
"""

from .meta_sync import _purchases, to_usd

# Minimum data before a rule is allowed to fire (avoids noise on day 1).
MIN_IMPR_CTR = 2000
MIN_IMPR_FREQ = 1000
MIN_PURCHASES_ROAS = 3


def normalise(row: dict, currency: str, level: str, names: dict[str, str], statuses: dict[str, str]) -> dict:
    """Insights row -> flat metrics dict in USD."""
    oid = row.get(f"{level}_id") or row.get("campaign_id") or ""
    spend = to_usd(float(row.get("spend", 0)), currency)
    imp = int(row.get("impressions", 0))
    clk = int(row.get("inline_link_clicks", 0))
    pur, rev = _purchases(row)
    rev = to_usd(rev, currency)
    return {
        "level": level,
        "object_id": oid,
        "parent_id": row.get("adset_id", "") if level == "ad" else "",
        "name": row.get(f"{level}_name") or names.get(oid, oid),
        "effective_status": statuses.get(oid, ""),
        "spend_usd": round(spend, 2),
        "impressions": imp,
        "clicks": clk,
        "purchases": pur,
        "revenue_usd": round(rev, 2),
        "frequency": round(float(row.get("frequency", 0) or 0), 2),
        "ctr": round(clk / imp, 4) if imp else 0.0,
        "cpa_usd": round(spend / pur, 2) if pur else 0.0,
        "roas": round(rev / spend, 2) if spend else 0.0,
    }


def evaluate(metrics: list[dict], targets: dict, plan_daily_usd: float, days_active: int,
             campaign_status: str, ad_statuses: list[dict]) -> list[dict]:
    """Compare normalised metrics (all levels) with the plan's monitoring targets."""
    findings: list[dict] = []

    if campaign_status not in ("ACTIVE", "IN_PROCESS", "WITH_ISSUES"):
        return findings  # paused / archived campaigns produce no alerts

    max_cpa = float(targets.get("max_cpa_usd", 0) or 0)
    min_ctr = float(targets.get("min_ctr", 0) or 0)
    max_freq = float(targets.get("max_frequency", 0) or 0)
    min_roas = float(targets.get("min_roas", 0) or 0)

    # Delivery problems from Meta itself (rejected ads, learning limited, etc.)
    for ad in ad_statuses:
        st = ad.get("effective_status", "")
        # ADSET_PAUSED / CAMPAIGN_PAUSED are deliberate (often by this app), not delivery problems.
        if st in ("DISAPPROVED", "WITH_ISSUES", "PENDING_REVIEW"):
            sev = "critical" if st == "DISAPPROVED" else "warning" if st == "WITH_ISSUES" else "info"
            issues = "; ".join(i.get("error_summary", "") for i in ad.get("issues_info", []) if i.get("error_summary"))
            findings.append(_f("delivery", sev, "ad", ad["id"], ad.get("name", ad["id"]),
                               f"Ad status is {st}" + (f": {issues}" if issues else ""),
                               {"effective_status": st}))

    for m in metrics:
        lvl, name = m["level"], m["name"]
        # Paused ad sets / ads still carry 7 days of history; judging them re-raises alerts
        # on things already acted on.
        if lvl != "campaign" and (m.get("effective_status") or "") not in ("", "ACTIVE"):
            continue

        # Pacing (campaign level only)
        if lvl == "campaign" and plan_daily_usd > 0:
            window = min(days_active, 7)
            if window >= 1:
                expected = plan_daily_usd * window
                ratio = m["spend_usd"] / expected if expected else 0
                if m["impressions"] == 0 and days_active >= 1:
                    findings.append(_f("no_delivery", "critical", lvl, m["object_id"], name,
                                       "Campaign is active but has no impressions in the last 7 days.", m))
                elif ratio < 0.6:
                    findings.append(_f("under_pacing", "warning", lvl, m["object_id"], name,
                                       f"Spending {ratio:.0%} of planned pace (${m['spend_usd']} vs ${expected:.0f} expected over {window} days).",
                                       {**m, "expected_spend_usd": round(expected, 2), "pace_ratio": round(ratio, 2)}))
                elif ratio > 1.3:
                    findings.append(_f("over_pacing", "warning", lvl, m["object_id"], name,
                                       f"Spending {ratio:.0%} of planned pace (${m['spend_usd']} vs ${expected:.0f} expected).",
                                       {**m, "expected_spend_usd": round(expected, 2), "pace_ratio": round(ratio, 2)}))

        # CPA: fires once spend is meaningful
        if max_cpa > 0 and m["spend_usd"] >= max_cpa:
            if m["purchases"] == 0:
                sev = "critical" if m["spend_usd"] >= 2 * max_cpa else "warning"
                findings.append(_f("no_purchases", sev, lvl, m["object_id"], name,
                                   f"${m['spend_usd']} spent with 0 purchases (target CPA ${max_cpa}).", m))
            elif m["cpa_usd"] > max_cpa * 1.25:
                sev = "critical" if m["cpa_usd"] > max_cpa * 2 else "warning"
                findings.append(_f("high_cpa", sev, lvl, m["object_id"], name,
                                   f"CPA ${m['cpa_usd']} is above target ${max_cpa}.", m))

        # CTR
        if min_ctr > 0 and m["impressions"] >= MIN_IMPR_CTR and m["ctr"] < min_ctr:
            findings.append(_f("low_ctr", "warning", lvl, m["object_id"], name,
                               f"CTR {m['ctr']:.2%} is below target {min_ctr:.2%}.", m))

        # Frequency (creative fatigue)
        if max_freq > 0 and m["impressions"] >= MIN_IMPR_FREQ and m["frequency"] > max_freq:
            findings.append(_f("high_frequency", "warning", lvl, m["object_id"], name,
                               f"Frequency {m['frequency']} exceeds {max_freq}; audience is saturating.", m))

        # ROAS
        if min_roas > 0 and m["purchases"] >= MIN_PURCHASES_ROAS and m["roas"] < min_roas:
            findings.append(_f("low_roas", "warning", lvl, m["object_id"], name,
                               f"ROAS {m['roas']}x is below target {min_roas}x.", m))

        # Positive signal: scaling candidate
        if lvl == "adset" and max_cpa > 0 and m["purchases"] >= 5 and m["cpa_usd"] <= max_cpa * 0.8:
            findings.append(_f("scale_candidate", "info", lvl, m["object_id"], name,
                               f"CPA ${m['cpa_usd']} is well under target with {m['purchases']} purchases; candidate for +20% budget.", m))

    findings += evaluate_creatives(metrics, max_cpa)
    findings += evaluate_rebalance(metrics, max_cpa)
    return findings


def evaluate_rebalance(metrics: list[dict], max_cpa: float) -> list[dict]:
    """Move budget from the weakest active ad set to the strongest one, total unchanged.
    Strongest: CPA at or below 80% of target with 3+ purchases. Weakest: CPA above 125% of target,
    or no purchases after spending one target CPA."""
    if max_cpa <= 0:
        return []
    sets = [m for m in metrics if m["level"] == "adset" and (m.get("effective_status") or "ACTIVE") == "ACTIVE"]
    good = [m for m in sets if m["purchases"] >= 3 and m["cpa_usd"] <= max_cpa * 0.8]
    bad = [m for m in sets if (m["purchases"] == 0 and m["spend_usd"] >= max_cpa)
           or (m["purchases"] > 0 and m["cpa_usd"] > max_cpa * 1.25)]
    if not good or not bad:
        return []
    best = min(good, key=lambda m: m["cpa_usd"])
    worst = max(bad, key=lambda m: (m["purchases"] == 0, m["cpa_usd"], m["spend_usd"]))
    if best is worst:
        return []
    w_desc = f"${worst['spend_usd']} spent, no orders" if not worst["purchases"] else f"CPA ${worst['cpa_usd']}"
    return [_f("rebalance", "warning", "adset", worst["object_id"], worst["name"],
               f"Move budget from '{worst['name']}' ({w_desc}) to '{best['name']}' (CPA ${best['cpa_usd']}, "
               f"{best['purchases']} orders); the total daily budget stays the same.",
               {**worst, "to_id": best["object_id"], "to_name": best["name"], "to_cpa_usd": best["cpa_usd"]})]


# Creative test thresholds
MIN_IMPR_PER_AD = 1500
WINNER_MIN_PURCHASES = 3
LOSER_CTR_RATIO = 0.6      # loser CTR < 60% of the winner's
LOSER_CPA_RATIO = 1.5      # loser CPA > 1.5x the winner's


def evaluate_creatives(metrics: list[dict], max_cpa: float) -> list[dict]:
    """Compare ads inside each ad set once every ad has enough impressions.
    Winner: best CPA with >= 3 purchases, else best CTR (if nobody converts yet).
    Losers: clearly worse than the winner on the same measure, with meaningful spend."""
    findings: list[dict] = []
    by_adset: dict[str, list[dict]] = {}
    for m in metrics:
        if m["level"] == "ad" and m.get("parent_id") and m.get("effective_status", "ACTIVE") in ("ACTIVE", ""):
            by_adset.setdefault(m["parent_id"], []).append(m)

    for adset_id, ads in by_adset.items():
        if len(ads) < 2 or any(a["impressions"] < MIN_IMPR_PER_AD for a in ads):
            continue
        converters = [a for a in ads if a["purchases"] >= WINNER_MIN_PURCHASES]
        if converters:
            winner = min(converters, key=lambda a: a["cpa_usd"])
            measure = "cpa"
        else:
            winner = max(ads, key=lambda a: a["ctr"])
            measure = "ctr"
        losers = []
        for a in ads:
            if a is winner:
                continue
            if measure == "cpa":
                worse = (a["purchases"] == 0 and a["spend_usd"] >= winner["cpa_usd"] * 2) or \
                        (a["purchases"] > 0 and a["cpa_usd"] > winner["cpa_usd"] * LOSER_CPA_RATIO and a["spend_usd"] >= winner["cpa_usd"] * 2)
            else:
                worse = a["ctr"] < winner["ctr"] * LOSER_CTR_RATIO
            if worse:
                losers.append(a)
        if not losers:
            continue
        win_desc = (f"CPA ${winner['cpa_usd']} ({winner['purchases']} purchases)" if measure == "cpa"
                    else f"CTR {winner['ctr']:.2%}")
        findings.append(_f("creative_winner", "info", "ad", winner["object_id"], winner["name"],
                           f"Winning creative in its ad set: {win_desc}; {len(losers)} losing ad(s) identified.",
                           {**winner, "measure": measure, "losers": [l["name"] for l in losers]}))
        for l in losers:
            lose_desc = (f"CPA ${l['cpa_usd']}" if l["purchases"] else f"${l['spend_usd']} with 0 purchases") if measure == "cpa" \
                else f"CTR {l['ctr']:.2%}"
            findings.append(_f("creative_loser", "warning", "ad", l["object_id"], l["name"],
                               f"Losing creative: {lose_desc} vs winner '{winner['name']}' ({win_desc}).",
                               {**l, "measure": measure, "winner": winner["name"]}))
    return findings


def _f(kind, severity, level, object_id, name, message, metrics) -> dict:
    return {"kind": kind, "severity": severity, "level": level, "object_id": object_id,
            "object_name": name, "message": message,
            "metrics": {k: v for k, v in metrics.items() if k not in ("level", "object_id", "name")}}


# Canned suggestions used when no API key is configured (or as a fallback).
# "action" is the machine-executable part (see actions.concretize); omitted = manual only.
TEMPLATE_SUGGESTIONS = {
    "delivery": [
        {"action": "Open the ad in Ads Manager and fix the policy issue, then request review",
         "expected_effect": "Delivery resumes within hours of approval", "risk": "None"},
        {"action": "Duplicate the ad with 'inspired by' wording and own photos",
         "expected_effect": "Avoids repeat rejection", "risk": "Loses learning on the old ad"},
    ],
    "no_delivery": [
        {"action": "Check ad set is not paused, budget >= minimum, and payment method is valid",
         "expected_effect": "Delivery starts within 1-2 hours", "risk": "None"},
    ],
    "under_pacing": [
        {"action": "Raise daily budget 20% on the best ad set or broaden targeting",
         "expected_effect": "Spend reaches plan within 2 days; CPA may rise 5-10%", "risk": "Slight CPA increase"},
        {"action": "Keep as is if CPA is on target; extend end date instead",
         "expected_effect": "Same orders over a longer period", "risk": "Misses the date target"},
    ],
    "over_pacing": [
        {"action": "Lower daily budgets to plan or set a campaign spending limit",
         "expected_effect": "Spend returns to plan from tomorrow", "risk": "Fewer impressions"},
    ],
    "no_purchases": [
        {"action": "Pause this object and move budget to the ad set with purchases",
         "expected_effect": "Stops waste immediately", "risk": "May pause something still in learning",
         "exec": {"type": "pause"}},
        {"action": "Check pixel fires on purchase (Events Manager test events)",
         "expected_effect": "Confirms whether purchases are happening but untracked", "risk": "None"},
    ],
    "high_cpa": [
        {"action": "Cut budget 30% here and shift to lowest-CPA ad set",
         "expected_effect": "Blended CPA drops toward target in 2-3 days", "risk": "Less volume short term",
         "exec": {"type": "budget_pct", "pct": -30}},
        {"action": "Pause this ad and let the others carry the ad set",
         "expected_effect": "Stops the worst performer", "risk": "Less reach", "exec": {"type": "pause"}},
        {"action": "Swap in the next creative variant",
         "expected_effect": "CTR up, CPA down if creative was the issue", "risk": "Resets learning"},
    ],
    "rebalance": [
        {"action": "Move 20% of this ad set's daily budget to the best ad set",
         "expected_effect": "More orders for the same total spend; the blended CPA drops toward the best ad set's",
         "risk": "The weaker ad set learns slower", "exec": {"type": "move_budget", "pct": 20}},
        {"action": "Pause this ad set and let the best one take its share",
         "expected_effect": "Stops the weakest spend at once", "risk": "Less audience variety",
         "exec": {"type": "pause"}},
    ],
    "creative_winner": [
        {"action": "Clone this winning ad (same creative) into the other ad sets, paused, so you can activate it there",
         "expected_effect": "Winner reaches every audience; usually lifts ad-set CTR 10-30%", "risk": "Creative fatigue sooner",
         "exec": {"type": "clone_winner"}},
        {"action": "Generate a fresh variant of this winner's visual and add it as a paused ad",
         "expected_effect": "Extends the winner's life before frequency climbs", "risk": "Image cost; needs review",
         "exec": {"type": "refresh_creative"}},
    ],
    "creative_loser": [
        {"action": "Pause this ad so budget flows to the winner",
         "expected_effect": "Ad-set CPA moves toward the winner's", "risk": "Less creative diversity",
         "exec": {"type": "pause"}},
        {"action": "Replace it with a new variant (fresh AI image, same copy) as a paused ad",
         "expected_effect": "Keeps 2+ ads in the ad set for the algorithm", "risk": "Image cost",
         "exec": {"type": "refresh_creative"}},
    ],
    "low_ctr": [
        {"action": "Replace the hook / first 3 seconds; test a new thumbnail",
         "expected_effect": "CTR +20-40% if the hook was the issue", "risk": "None"},
        {"action": "Generate a fresh AI variant of this ad's image and add it as a paused ad",
         "expected_effect": "New visual, same copy; review before activating", "risk": "Image cost",
         "exec": {"type": "refresh_creative"}},
        {"action": "Pause this ad while the new creative is prepared",
         "expected_effect": "Budget flows to better ads", "risk": "Less reach", "exec": {"type": "pause"}},
    ],
    "high_frequency": [
        {"action": "Add 2 fresh creatives and widen audience or add a lookalike",
         "expected_effect": "Frequency drops below target within a week", "risk": "CPM may rise briefly"},
        {"action": "Generate a fresh AI image variant for this ad and add it as a paused ad",
         "expected_effect": "New creative resets fatigue once activated", "risk": "Image cost",
         "exec": {"type": "refresh_creative"}},
        {"action": "Cut this ad set's budget 20% until new creatives are live",
         "expected_effect": "Slows saturation", "risk": "Fewer impressions", "exec": {"type": "budget_pct", "pct": -20}},
    ],
    "low_roas": [
        {"action": "Raise AOV with a bundle/upsell on the landing page or cut spend on this object",
         "expected_effect": "ROAS improves without changing ads", "risk": "Lower conversion rate"},
        {"action": "Cut this ad set's budget 20%",
         "expected_effect": "Less spend at low return", "risk": "Fewer orders", "exec": {"type": "budget_pct", "pct": -20}},
    ],
    "scale_candidate": [
        {"action": "Increase this ad set's daily budget by 20% (not more, to protect learning)",
         "expected_effect": "~20% more purchases at similar CPA", "risk": "CPA may rise 5-10%",
         "exec": {"type": "budget_pct", "pct": 20}},
        {"action": "Duplicate the ad set with a 2x budget as a separate test",
         "expected_effect": "Faster scale while the original stays stable", "risk": "Audience overlap"},
    ],
}


from .tracking import SUGGESTIONS as _TRACKING  # noqa: E402

TEMPLATE_SUGGESTIONS["tracking"] = _TRACKING
