"""Campaign plan generation.

Claude produces a structured plan JSON from the estimate + the learned
strategy profile. If no API key is configured, a template plan is returned
so the app still works end-to-end during development.
"""

import json
from typing import Any

from .benchmarks import get_niche

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "objective": {"type": "string"},
        "phases": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "days": {"type": "integer"},
                    "budget_pct": {"type": "number"},
                    "goal": {"type": "string"},
                },
                "required": ["name", "days", "budget_pct", "goal"],
                "additionalProperties": False,
            },
        },
        "ad_sets": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "audience": {"type": "string"},
                    "placements": {"type": "string"},
                    "daily_budget_pct": {"type": "number"},
                    "optimization": {"type": "string"},
                },
                "required": ["name", "audience", "placements", "daily_budget_pct", "optimization"],
                "additionalProperties": False,
            },
        },
        "creatives": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "format": {"type": "string"},
                    "hook": {"type": "string"},
                    "primary_text": {"type": "string"},
                    "headline": {"type": "string"},
                    "cta": {"type": "string"},
                    "visual_prompt": {"type": "string"},
                },
                "required": ["format", "hook", "primary_text", "headline", "cta", "visual_prompt"],
                "additionalProperties": False,
            },
        },
        "kill_rules": {"type": "array", "items": {"type": "string"}},
        "scale_rules": {"type": "array", "items": {"type": "string"}},
        "monitoring_targets": {
            "type": "object",
            "properties": {
                "max_cpa_usd": {"type": "number"},
                "min_ctr": {"type": "number"},
                "max_frequency": {"type": "number"},
                "min_roas": {"type": "number"},
            },
            "required": ["max_cpa_usd", "min_ctr", "max_frequency", "min_roas"],
            "additionalProperties": False,
        },
        "risks": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "summary", "objective", "phases", "ad_sets", "creatives",
        "kill_rules", "scale_rules", "monitoring_targets", "risks",
    ],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You are a senior Meta Ads media buyer who plans e-commerce campaigns
for a Pakistan-based jackets / upperwear brand shipping to foreign countries. You write
complete, concrete, launch-ready plans. Be specific: real audience definitions, real hooks,
real numbers. Budgets must add up to 100%. Respect the estimate's warnings and the NICHE
GUIDANCE (it tells you how this product category behaves and its policy risks). Keep text tight.

Money: when the ESTIMATE has break_even_cpa_usd > 0, that is what one order may cost before it
loses money. Set monitoring_targets.max_cpa_usd at or below it (about 85-90% of it), and
min_roas at or above price / margin. If the estimated cpa_usd is above it, say so in risks and
plan the test phase to find cheaper audiences before scaling.

Season: the ESTIMATE's `season` lists the sales events inside this campaign (Black Friday,
the Christmas shipping cutoff, Boxing Day...). Time the phases around them: test before an event,
put the most budget on the event days, and write at least one creative for the event (with the
shipping cutoff date when it matters). In off season, keep tests small.

Retargeting: when INPUTS.has_pixel is true, include exactly one ad set named "Retargeting" with
10-20% of the daily budget, audience "Store visitors and add-to-cart in the last 30 days, excluding
buyers"; its creative angle is a reminder or offer. The app builds that audience and keeps recent
buyers out of every other ad set automatically.

For each creative also write `visual_prompt`: a one-paragraph brief for an image generator
that will be given the real product photo as reference. Describe scene, model/pose or flat-lay,
lighting, mood, background, and composition for the stated format. Never ask for text, logos,
celebrity faces or copyrighted characters in the image; say "the jacket from the reference
photo, unchanged" so the product stays accurate.

The user has a strategy profile learned from their past edits. Treat each rule in it as a
strong preference: follow it unless it would clearly hurt results, and if you deviate,
say why in `risks`."""


def _user_prompt(inputs: dict, estimate: dict, rules: list[str]) -> str:
    rules_text = "\n".join(f"- {r}" for r in rules) if rules else "- (none yet)"
    niche = get_niche(inputs["niche"])
    return f"""INPUTS
{json.dumps(inputs, indent=2)}

NICHE GUIDANCE ({niche['label']})
{niche['guidance']}

ESTIMATE (benchmark-based unless source says account_history)
{json.dumps(estimate, indent=2)}

USER STRATEGY PROFILE (learned from past approvals/edits)
{rules_text}

Produce the full campaign plan for Meta (Facebook + Instagram)."""


def generate_plan(inputs: dict, estimate: dict, rules: list[str]) -> dict:
    from . import llm, usage
    if not llm.configured():
        return _template_plan(inputs, estimate, rules)
    try:
        raw = llm.structured(SYSTEM_PROMPT, _user_prompt(inputs, estimate, rules), PLAN_SCHEMA, "Plans", effort="high")
        return normalize_plan(raw, inputs.get("duration_days"))
    except usage.BudgetExceeded as ex:
        note = str(ex)
    except llm.LLMError as ex:
        note = f"The AI plan failed ({ex}), so this is the template plan. Try again in a minute."
    except PlanError as ex:
        note = f"The AI plan was not usable ({ex}), so this is the template plan. Try again in a minute."
    plan = _template_plan(inputs, estimate, rules)
    plan["risks"] = [note] + plan["risks"]
    return plan


def _test_days(d: int) -> int:
    """About 30% of the campaign, at least 3 days, and always leaving at least 1 day to scale."""
    return max(1, min(max(int(d * 0.3), 3), d - 1))


def _template_plan(inputs: dict, estimate: dict, rules: list[str]) -> dict:
    """Deterministic fallback used when no AI provider is configured."""
    product = inputs["product"]
    country = estimate["assumptions"]["country"]
    cpa = estimate["cpa_usd"] or 10
    margin = estimate.get("break_even_cpa_usd") or 0
    celeb = inputs["niche"] == "celebrity_jackets"
    if celeb:
        interest_audience = (f"{country}, 18-40, fans of the film/series/artist behind '{product}', "
                             "cosplay, comic-con, pop-culture pages")
        hook = f"The jacket you saw on screen - '{product}' inspired, now shipping to {country}"
        niche_risks = ["Celebrity/movie niche: do not use official stills, logos or the actor's face; "
                       "Meta rejects trademark/likeness use. Use 'inspired by' wording and own photos."]
    else:
        interest_audience = (f"{country}, 22-45, interests: leather jackets, motorcycle apparel, "
                             "streetwear, AllSaints / Schott / Zara shoppers")
        hook = f"Stop scrolling - this is the {product} everyone is asking about"
        niche_risks = ["Leather niche: emphasise material quality, fit guide and returns; "
                       "peak demand Sept-Jan, expect higher CPMs in Q4."]
    return {
        "summary": (
            f"[TEMPLATE - no API key] {inputs['duration_days']}-day sales campaign for '{product}' in {country}. "
            f"Expected {estimate['orders_low']}-{estimate['orders_high']} orders at ~${cpa} CPA."
        ),
        "objective": "Sales (Conversions) - optimise for Purchase",
        "phases": [
            {"name": "Testing", "days": _test_days(inputs["duration_days"]), "budget_pct": 30,
             "goal": "Find 2 winning creatives and 1 winning audience"},
            {"name": "Scaling", "days": inputs["duration_days"] - _test_days(inputs["duration_days"]),
             "budget_pct": 70, "goal": "Scale winners by 20% every 2 days while CPA holds"},
        ],
        "ad_sets": [
            {"name": "Broad", "audience": f"{country}, 22-50, all genders, no interests (Advantage+)",
             "placements": "Advantage+ placements", "daily_budget_pct": 40, "optimization": "Purchase"},
            {"name": "Interest stack", "audience": interest_audience,
             "placements": "Feeds + Reels + Stories", "daily_budget_pct": 45 if inputs.get("has_pixel") else 35, "optimization": "Purchase"},
            ({"name": "Retargeting", "audience": "Store visitors and add-to-cart in the last 30 days, excluding buyers",
              "placements": "Advantage+ placements", "daily_budget_pct": 15, "optimization": "Purchase"}
             if inputs.get("has_pixel") else
             {"name": "Lookalike/Engagers", "audience": "1% LAL of purchasers (or IG/FB engagers if no pixel data)",
              "placements": "Advantage+ placements", "daily_budget_pct": 25, "optimization": "Purchase"}),
        ],
        "creatives": [
            {"format": "Static image 9:16", "hook": hook,
             "primary_text": f"{product} now shipping to {country}. Limited stock. Free delivery this week.",
             "headline": f"{product} - Order Today", "cta": "Shop Now",
             "visual_prompt": f"Full-body street photo of a model wearing the jacket from the reference photo, unchanged. "
                              f"Evening city street, warm neon reflections, shallow depth of field, confident stance, "
                              f"vertical 9:16 composition with space at the top. No text, no logos."},
            {"format": "Static image 1:1", "hook": "Material and craftsmanship close-up",
             "primary_text": f"Tired of the usual? Meet {product}.", "headline": "Shop the collection", "cta": "Shop Now",
             "visual_prompt": "Studio product shot of the jacket from the reference photo, unchanged, on an invisible "
                              "mannequin, dark grey seamless background, soft key light from the left, visible leather "
                              "grain and stitching, square composition, centered. No text."},
            {"format": "Static image 4:5", "hook": "Styled outfit, lifestyle",
             "primary_text": "Swipe to see why.", "headline": f"{product}", "cta": "Shop Now",
             "visual_prompt": "Lifestyle photo, model wearing the jacket from the reference photo, unchanged, leaning on "
                              "a vintage motorcycle at golden hour, muted earthy tones, 4:5 portrait framing, natural "
                              "light, candid feel. No text, no brand logos."},
        ],
        "kill_rules": [
            f"Pause any ad with spend > 2x target CPA (${cpa * 2:.0f}) and 0 purchases",
            "Pause ads with CTR < 0.8% after 2,000 impressions",
            "Pause ad sets with frequency > 3.0 in a 7-day window",
        ],
        "scale_rules": [
            "Increase budget 20% every 48h on ad sets with CPA below target for 3 consecutive days",
            "Duplicate winning ad into Broad ad set once it has 10+ purchases",
        ],
        "monitoring_targets": {
            "max_cpa_usd": round(min(cpa * 1.3, margin * 0.9), 2) if margin > 0 else round(cpa * 1.3, 2),
            "min_ctr": 0.009,
            "max_frequency": 3.0,
            "min_roas": round(inputs["price_usd"] / margin, 2) if margin > 0 else (round(inputs["price_usd"] / cpa, 2) if cpa else 2.0),
        },
        "risks": ["Template plan: add a Gemini or Anthropic key for an AI-generated plan."]
        + [f"{e['name']} on {e['date']}: {e['note']}" for e in estimate.get("season", {}).get("events", [])]
        + ([f"Estimated ${cpa:.2f} per order is above the ${margin:.2f} you keep per order"] if 0 < margin < cpa else [])
        + niche_risks
        + ([f"Applied learned rules: {len(rules)}"] if rules else []),
    }


# ---------- one cleanup for every plan, from the AI or from a user's edit ----------

class PlanError(ValueError):
    pass


def _num(v, field: str, percent_as_fraction: bool = False) -> float:
    """40, 40.0, "40", "40%", "$45", "1,200" -> number. With percent_as_fraction, "1.2%" -> 0.012."""
    if isinstance(v, bool):
        raise PlanError(f"{field} must be a number")
    if isinstance(v, (int, float)):
        return float(v)
    raw = str(v or "").strip()
    s = raw.replace(",", "").replace("$", "").replace("%", "").strip()
    try:
        n = float(s)
    except ValueError:
        raise PlanError(f"{field} must be a number, got {v!r}") from None
    if n != n or n in (float("inf"), float("-inf")):
        raise PlanError(f"{field} must be a number, got {v!r}")
    return n / 100 if percent_as_fraction and raw.endswith("%") else n


def _fit_days(phases: list[dict], total: int) -> None:
    """Scale phase days so they add up to the campaign length exactly, each at least 1 day."""
    if len(phases) > total:
        del phases[total:]
    have = sum(p["days"] for p in phases)
    if have == total or not phases:
        return
    for p in phases:
        p["days"] = max(1, int(p["days"] * total / have)) if have else 1
    diff = total - sum(p["days"] for p in phases)
    if diff > 0:
        max(phases, key=lambda p: p["days"])["days"] += diff
    while diff < 0:  # take from the longest phase first; len(phases) <= total so this ends
        p = max(phases, key=lambda p: p["days"])
        step = min(p["days"] - 1, -diff)
        p["days"] -= step
        diff += step


def normalize_plan(c: dict, duration_days: int | None = None) -> dict:
    """Keep only the schema's fields, turn numbers written as text into numbers, put shares on a
    0-100 scale and CTR on a 0-1 scale. Raises PlanError when the plan cannot be used."""
    if not isinstance(c, dict):
        raise PlanError("the plan must be an object")
    props = PLAN_SCHEMA["properties"]
    missing = [k for k in PLAN_SCHEMA["required"] if k not in c]
    if missing:
        raise PlanError(f"missing {', '.join(missing)}")
    out: dict = {}
    for key, spec in props.items():
        v = c.get(key)
        if spec["type"] == "string":
            out[key] = str(v if v is not None else "").strip()
        elif spec["type"] == "array" and spec["items"]["type"] == "string":
            if isinstance(v, str):
                v = [x for x in v.split("\n")]
            if not isinstance(v, list):
                raise PlanError(f"'{key}' must be a list")
            out[key] = [str(x).strip() for x in v if str(x).strip() and not isinstance(x, (dict, list))]
        elif spec["type"] == "array":
            if not isinstance(v, list):
                raise PlanError(f"'{key}' must be a list")
            fields = spec["items"]["properties"]
            items = []
            for item in v:
                if not isinstance(item, dict):
                    raise PlanError(f"items of '{key}' must be objects")
                clean = {}
                for f, fs in fields.items():
                    x = item.get(f)
                    if fs["type"] in ("number", "integer"):
                        n = _num(x if x not in (None, "") else 0, f"{key}.{f}")
                        clean[f] = int(round(n)) if fs["type"] == "integer" else n
                    else:
                        clean[f] = str(x if x is not None else "").strip()
                items.append(clean)
            out[key] = items
        else:  # monitoring_targets
            if not isinstance(v, dict):
                raise PlanError("'monitoring_targets' must be an object")
            out[key] = {f: _num(v.get(f, 0), f, percent_as_fraction=(f == "min_ctr")) for f in spec["properties"]}
            ctr_was_percent = isinstance(v.get("min_ctr"), str) and v["min_ctr"].strip().endswith("%")
    if not out["ad_sets"]:
        raise PlanError("a plan needs at least one ad set")
    # shares written as fractions (0.4) instead of percentages (40)
    for key, f in (("ad_sets", "daily_budget_pct"), ("phases", "budget_pct")):
        vals = [i[f] for i in out[key]]
        if vals and sum(vals) <= 1.0001 and any(x > 0 for x in vals):
            for i in out[key]:
                i[f] = round(i[f] * 100, 2)
        for i in out[key]:
            if i[f] < 0:
                raise PlanError(f"{key}.{f} cannot be negative")
    t = out["monitoring_targets"]
    if t["min_ctr"] >= 0.2 and not ctr_was_percent:  # "1" or "1.2" meant percent
        t["min_ctr"] = round(t["min_ctr"] / 100, 5)
    for p in out["phases"]:
        p["days"] = max(int(p["days"]), 1)
    if duration_days and out["phases"]:
        _fit_days(out["phases"], duration_days)
    return out
