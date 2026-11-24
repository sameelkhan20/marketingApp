"""Campaign plan generation.

Claude produces a structured plan JSON from the estimate + the learned
strategy profile. If no API key is configured, a template plan is returned
so the app still works end-to-end during development.
"""

import json
import re
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
                    "angle": {"type": "string"},
                    "hook": {"type": "string"},
                    "primary_text": {"type": "string"},
                    "headline": {"type": "string"},
                    "cta": {"type": "string"},
                    "visual_prompt": {"type": "string"},
                },
                "required": ["format", "angle", "hook", "primary_text", "headline", "cta", "visual_prompt"],
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
for a Pakistan-based online store selling to foreign countries and inside Pakistan. The store sells
many kinds of products; INPUTS.niche is this product's category and INPUTS.category_note the user's own
description of it (use it when present). You write
complete, concrete, launch-ready plans. Be specific: real audience definitions, real hooks,
real numbers. Budgets must add up to 100%. Respect the estimate's warnings and the CATEGORY
GUIDANCE (it tells you how this product category behaves and its policy risks). Keep text tight.
Units: phases.budget_pct and ad_sets.daily_budget_pct are percentages from 0 to 100;
monitoring_targets.min_ctr is a fraction (0.01 means 1%).

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

Selling: SALES BRIEF holds the facts that sell this product (offer, why buy, delivery days,
returns, reviews). Use them; never invent a discount, a review count, a rating or a delivery promise
that is not in it. Lead the strongest creative with the offer when there is one. Give every
creative a different `angle`, one of: "offer", "quality", "gift", "problem-solution",
"social-proof", "occasion" (an event in the estimate), "scarcity". Hooks must be specific to this
product (material, size, count, the offer, the delivery time, the occasion): never generic
openers like "Elevate", "Upgrade your", "Discover" or "Stop scrolling". Primary text: the benefit,
then the proof, then the offer and delivery/returns, then the call to action; short lines.
When YOUR WINNERS lists ads that sold for this store, reuse what worked there (angles, hooks,
audiences) and say so in `summary`.

PLAYBOOK (proven direct-response principles, use them deliberately):
- Awareness: cold broad audiences mostly do not know the product: open with the problem, the
  desire or the occasion, then the product as the answer. Interest audiences know the category:
  lead with what makes this one better. Retargeting already knows it: lead with the offer, proof,
  a reason to act now and removal of risk (delivery time, returns).
- Offer value: perceived value rises with a bigger outcome, more certainty it will work (proof,
  guarantee, returns), less waiting (fast delivery) and less effort; a bundle, a bonus or a gift
  box can beat a discount. Only use what the SALES BRIEF states.
- Persuasion: specific social proof (counts, ratings, real reviews), honest scarcity or deadlines
  (stock, event cutoffs), authority (material grade, craftsmanship, years in business), and
  giving first (size guide, care tips). Never fake any of them.
- Copy: specific facts and numbers sell, vague praise does not; one idea per creative; the first
  line must stop the scroll for THIS buyer; say who it is for.
- Reach: prefer broad audiences and many distinct creatives over narrow targeting; keep a
  consistent look (colours, framing) so the brand becomes recognisable.
- The job: write for the job the buyer hires the product to do (look sharp at an event, host
  guests proudly, give a gift that impresses), not only for its features.
Map creatives to stages: at least one for cold buyers (problem/desire), one for warm buyers
(why this one), and the retargeting creative for hot buyers (offer and proof).

Meta Advertising Standards (breaking them gets ads rejected and ad accounts restricted):
never use another brand's name or words like replica, first copy, master copy, AAA quality or dupe;
never assert the reader's personal traits (no "Are you overweight/single/Muslim...?"); no health,
weight-loss, before/after or income claims; no "guaranteed", "#1" or "risk-free" unless the brief
proves it; no fake urgency or discounts the brief does not state; no shouting (!!! or ALL CAPS).
For celebrity-inspired products describe the style ("inspired by the classic biker look"), never
name the brand, the studio or the actor.

When instructions conflict, this order decides: (1) Meta Advertising Standards and the truth of
the SALES BRIEF; (2) formats (no video), the pixel (no Retargeting ad set without one) and Meta's
minimum daily budget per ad set (FACTS); (3) the money limits; (4) USER STRATEGY PROFILE rules;
(5) everything else.

Budget reality: Meta needs about 50 purchases a week per ad set to optimise well. When the
estimate implies far fewer, use fewer ad sets (one broad prospecting ad set plus retargeting is
often best) and put the variety into creatives instead.

Formats: the store makes no videos. Every creative's `format` is exactly one of "Carousel"
(2-10 square cards, the best for showing several angles or items), "Static image 1:1",
"Static image 4:5" or "Static image 9:16". Include at least one Carousel. For a Carousel, the
visual_prompt describes the card set (what each card shows: front, back, detail, in use...).

For each creative also write `visual_prompt`: a one-paragraph brief for an image generator
that will be given the real product photo as reference. Describe scene, model/pose or flat-lay,
lighting, mood, background, and composition for the stated format. Never ask for text, logos,
celebrity faces or copyrighted characters in the image; say "the product from the reference
photo, unchanged" so the product stays accurate.

The user has a strategy profile learned from their past edits. Treat each rule in it as a
strong preference: follow it unless it would clearly hurt results, and if you deviate,
say why in `risks`."""


BRIEF_FIELDS = {"offer": "Offer", "usp": "Why buy from us", "delivery_days": "Delivery time (days)",
                "returns": "Returns", "social_proof": "Reviews / social proof"}


def _brief_text(inputs: dict) -> str:
    lines = [f"- {label}: {inputs[k]}" for k, label in BRIEF_FIELDS.items() if inputs.get(k) not in (None, "", 0)]
    return "\n".join(lines) if lines else "- (nothing given: do not promise discounts, reviews or delivery times)"


def _winners_text(winners: list[dict] | None) -> str:
    if not winners:
        return "- (no ad history synced for this store yet)"
    out = []
    for w in winners[:8]:
        bits = [f"\"{w.get('name', '')}\"", f"{w.get('purchases', 0)} purchases", f"CPA ${w.get('cpa_usd', 0)}"]
        if w.get("roas"):
            bits.append(f"ROAS {w['roas']}x")
        if w.get("adset"):
            bits.append(f"ad set \"{w['adset']}\"")
        if w.get("title") or w.get("body"):
            bits.append(f"headline \"{(w.get('title') or '')[:80]}\" text \"{(w.get('body') or '')[:160]}\"")
        out.append("- " + ", ".join(bits))
    return "\n".join(out)


def _user_prompt(inputs: dict, estimate: dict, rules: list[str], winners: list[dict] | None = None) -> str:
    rules_text = "\n".join(f"- {r}" for r in rules) if rules else "- (none yet)"
    niche = get_niche(inputs["niche"])
    return f"""INPUTS
{json.dumps({k: v for k, v in inputs.items() if k not in BRIEF_FIELDS}, indent=2)}

SALES BRIEF
{_brief_text(inputs)}

YOUR WINNERS (this store's best past ads by purchases; reuse only those that sold the same kind of
product as this plan, judging by their names and text, and ignore the rest)
{_winners_text(winners)}

FACTS (do not create ad sets below the minimum: fewer ad sets is better than ad sets Meta cannot run)
{json.dumps(_facts(inputs, estimate), indent=2)}

CATEGORY GUIDANCE ({niche['label']})
{niche['guidance']}

ESTIMATE (benchmark-based unless source says account_history)
{json.dumps(estimate, indent=2)}

USER STRATEGY PROFILE (learned from past approvals/edits)
{rules_text}

Produce the full campaign plan for Meta (Facebook + Instagram)."""


REVIEW_PROMPT = """You are a strict senior Meta Ads reviewer at an agency. A planner wrote the
PLAN below for this store. Return the corrected, complete plan in the same JSON shape. Check and fix:
1. Money: ad set shares add up to 100; monitoring_targets.max_cpa_usd is at or below the estimate's
   break_even_cpa_usd when that is above 0; min_roas fits price / margin.
2. Budget reality (FACTS below): if purchases per ad set per week are low (under ~10), consolidate to
   at most one prospecting ad set plus retargeting, and move the variety into creatives.
3. Every ad set meets Meta's minimum daily budget in FACTS.
4. Creatives: every one has a different `angle`; at least one Carousel; no video formats; hooks are
   specific (no "Elevate", "Upgrade your", "Discover", "Stop scrolling"); the offer from SALES BRIEF
   leads the strongest creative; nothing is promised that the brief does not state.
5. Events in the estimate are used: phases timed around them, one creative for the biggest one,
   delivery cutoffs respected (DELIVERY DAYS in FACTS).
6. Category policy risks are in `risks`.
8. Meta Advertising Standards (in the planner's instructions): remove any brand name, counterfeit word,
   personal-attribute question, health/income claim or unproven absolute claim.
7. PLAYBOOK: creatives cover cold, warm and hot buyers; the retargeting creative leads with offer and
   proof; copy uses specific facts; nothing fake (no invented scarcity, reviews or authority).
USER RULES were learned from this user's own edits and results: keep the plan consistent with them,
unless that breaks something higher in this order: (1) Meta standards and the brief's truth, (2) no video,
no Retargeting without a pixel, every ad set at Meta's minimum budget, (3) money limits. Say so in a Review
line when a rule had to give way.
If INPUTS.has_pixel is false there must be no Retargeting ad set (it cannot run without a pixel).
Keep what is already good. Add 1-3 lines to `risks` that start with "Review:" and say what you changed
(or "Review: no changes needed")."""


def _facts(inputs: dict, estimate: dict) -> dict:
    """Numbers the reviewer needs that a model gets wrong when it has to work them out."""
    from .benchmarks import get_country
    days = max(int(inputs.get("duration_days") or 1), 1)
    expected = float((estimate.get("assumptions") or {}).get("orders_raw") or estimate.get("orders_expected") or 0)
    weekly = expected / days * 7
    try:
        cmin = get_country(inputs["country"])["min_daily_usd"]
    except KeyError:
        cmin = 5
    return {"daily_budget_usd": round(float(estimate.get("daily_usd") or 0), 2),
            "expected_purchases_per_week": round(weekly, 1),
            "meta_min_daily_per_ad_set_usd": cmin,
            "max_ad_sets_the_budget_supports": max(1, int(float(estimate.get("daily_usd") or 0) // (cmin * 2))),
            "delivery_days": inputs.get("delivery_days") or None}


def fit_to_budget(plan: dict, inputs: dict, estimate: dict) -> dict:
    """Fold ad sets that would get less than Meta's minimum into the largest one."""
    from .budget_fit import fit_ad_sets
    f = _facts(inputs, estimate)
    sets, folded = fit_ad_sets(plan.get("ad_sets", []), float(f["daily_budget_usd"] or 0), float(f["meta_min_daily_per_ad_set_usd"]))
    if folded:
        plan["ad_sets"] = sets
        plan["risks"] = list(plan.get("risks", [])) + [
            f"Budget: {', '.join(folded)} would get less than Meta's ${f['meta_min_daily_per_ad_set_usd']}/day minimum, "
            "so its share went to the largest ad set. A bigger daily budget brings it back."]
    return plan


def generate_plan(inputs: dict, estimate: dict, rules: list[str], winners: list[dict] | None = None) -> dict:
    return fit_to_budget(_generate_plan(inputs, estimate, rules, winners), inputs, estimate)


def _generate_plan(inputs: dict, estimate: dict, rules: list[str], winners: list[dict] | None = None) -> dict:
    from . import llm, usage
    from .config import PLAN_MODEL
    if not llm.configured():
        return _template_plan(inputs, estimate, rules)

    def ask(system: str, prompt: str, feature: str) -> dict:
        try:
            return llm.structured(system, prompt, PLAN_SCHEMA, feature, effort="high", model=PLAN_MODEL)
        except llm.LLMError as ex:
            if "404" in str(ex) or "not found" in str(ex).lower():  # the stronger model went away: use the default
                return llm.structured(system, prompt, PLAN_SCHEMA, feature, effort="high")
            raise
    try:
        draft = normalize_plan(ask(SYSTEM_PROMPT, _user_prompt(inputs, estimate, rules, winners), "Plans"),
                               inputs.get("duration_days"))
        # second pass: a reviewer checks money, budget reality, angles and events, and fixes them
        try:
            rules_text = "\n".join(f"- {r}" for r in rules) if rules else "- (none)"
            review_prompt = (f"PLAN\n{json.dumps(draft, indent=1)}\n\nFACTS\n{json.dumps(_facts(inputs, estimate), indent=1)}\n\n"
                             f"INPUTS.has_pixel: {bool(inputs.get('has_pixel'))}\n\nUSER RULES\n{rules_text}\n\n"
                             f"YOUR WINNERS\n{_winners_text(winners)}\n\n"
                             f"SALES BRIEF\n{_brief_text(inputs)}\n\nESTIMATE\n{json.dumps(estimate, indent=1)}\n\n"
                             f"CATEGORY GUIDANCE\n{get_niche(inputs['niche'])['guidance']}")
            return normalize_plan(ask(REVIEW_PROMPT, review_prompt, "Plan review"), inputs.get("duration_days"))
        except (llm.LLMError, PlanError, usage.BudgetExceeded) as ex:
            draft["risks"] = draft["risks"] + [f"Review step skipped ({ex}); this is the first draft."]
            return draft
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
    cat = get_niche(inputs["niche"])
    note = (inputs.get("category_note") or "").strip()
    interest_audience = f"{country}, {cat['audience']}" + (f" (product type: {note})" if note else "")
    hook = cat["hook"].format(product=product, country=country)
    offer = (inputs.get("offer") or "").strip()
    delivery = f" Delivered in {inputs['delivery_days']} days." if inputs.get("delivery_days") else ""
    returns = f" {inputs['returns'].strip()}." if (inputs.get("returns") or "").strip() else ""
    niche_risks = [cat["risk"]]
    what = "the product from the reference photo, unchanged"
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
            {"format": "Carousel", "angle": "offer" if offer else "quality", "hook": f"{offer}: {product}" if offer else hook,
             "primary_text": f"{product} now shipping to {country}.{(' ' + offer + '.') if offer else ''}{delivery}{returns}",
             "headline": f"{product} - Order Today", "cta": "Shop Now",
             "visual_prompt": f"Carousel of square cards showing {what}: card 1 the whole product "
                              f"({cat['studio'].lower()}), then other angles, a close-up of the details and the product "
                              f"in use ({cat['lifestyle'].lower()}). No text, no logos."},
            {"format": "Static image 1:1", "angle": "problem-solution" if not offer else "quality",
             "hook": "Quality and detail close-up",
             "primary_text": f"Tired of the usual? Meet {product}.", "headline": "Shop the collection", "cta": "Shop Now",
             "visual_prompt": f"{cat['studio']}, showing {what}. Square composition, centered. No text."},
            {"format": "Static image 4:5", "angle": "social-proof" if (inputs.get("social_proof") or "").strip() else "gift",
             "hook": (inputs.get("social_proof") or "").strip() or "Styled outfit, lifestyle",
             "primary_text": "Swipe to see why.", "headline": f"{product}", "cta": "Shop Now",
             "visual_prompt": f"Lifestyle photo: {cat['lifestyle'].lower()}, showing {what}, golden-hour light, "
                              f"4:5 portrait framing, candid feel. No text, no brand logos."},
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
    for c in out["creatives"]:  # the store makes no videos: a video idea becomes a carousel of stills
        f = c["format"].lower()
        if "carousel" in f or (not f.startswith("static image") and re.search(r"\b(video|reel|motion|gif)\b", f)):
            c["format"] = "Carousel"
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
