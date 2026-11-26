"""Auto plan: the owner gives only the store, the budget and the days; the app decides the rest from the
store's own data, and writes down why for every decision so the owner can check it before approving.

1. What to advertise: the whole catalogue (the product set whose catalogue ads sold best), or one product
   (the store's best seller, or the product with the best ad results), whichever the data favours.
2. Where: the country where this budget is expected to bring the most orders, from the store's own results
   per country (and its target markets, the US and the UK, when there is no data yet).
3. Price, category, and the sales brief (read from the store's own ads), then the normal plan writer.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

TARGET_MARKETS = ("US", "GB")   # the stores sell mostly to the US and the UK
GOOD_PRODUCT = ("Proven seller", "Best seller in your store", "One sale so far")
UNPROVEN_DISCOUNT = 0.7         # a market with no results of the store's own: its benchmark counts for less


class AutoPlanError(ValueError):
    pass


def _price(session: Session, brand_id: int, items: list[dict], niche: str) -> tuple[float, str]:
    """A price for the estimate: the store's average order from its synced results, else its products' middle
    price, else the category's usual one."""
    from .benchmarks import get_niche
    from .db import stat_rows
    rows = [r for r in stat_rows(session, brand_id) if r.purchases and r.revenue_usd]
    if rows:
        pur = sum(r.purchases for r in rows)
        aov = sum(r.revenue_usd for r in rows) / pur
        if aov > 0:
            return round(aov, 2), f"your average order in Meta's results (${aov:.2f} over {pur} sales)"
    prices = sorted(i["price_usd"] for i in items if i.get("price_usd"))
    if prices:
        mid = prices[len(prices) // 2]
        return mid, f"the middle price of your products (${mid:.2f})"
    typ = float(get_niche(niche).get("typical_price", 60))
    return typ, f"the usual price for this kind of product (${typ:.2f}); no sales or prices to go on yet"


def _country_options(session: Session, brand_id: int, budget_pkr: float, days: int, niche: str,
                     price: float) -> list[tuple[float, str, str]]:
    """[(orders expected, country, source)] for every candidate market, best first."""
    from .benchmarks import COUNTRY_BENCHMARKS
    from .db import stat_rows
    from .estimator import estimate_orders
    from .history import account_overrides
    from .outcomes import calibration_factor
    have = {r.country for r in stat_rows(session, brand_id) if r.impressions >= 5000 and r.country in COUNTRY_BENCHMARKS}
    best = []
    for c in sorted(have | set(TARGET_MARKETS)):
        ov = account_overrides(c, niche, price, brand_id)
        cal = (1.0, 0) if ov else calibration_factor(session, c, brand_id, niche)
        e = estimate_orders(budget_pkr, c, niche, price, days, overrides=ov, calibration=cal)
        orders = float(e.assumptions.get("orders_raw") or 0)
        if not ov and cal[1] == 0:
            # a market the store has never proven: a generic benchmark is not as good as its own results
            orders *= UNPROVEN_DISCOUNT
            source = "benchmark, cut by 30% because you have no results there yet"
        else:
            source = "your own results"
        best.append((orders, c, source))
    best.sort(reverse=True)
    return best


def _country(session: Session, brand_id: int, budget_pkr: float, days: int, niche: str, price: float) -> tuple[str, str]:
    """The country where this budget is expected to bring the most orders."""
    from .benchmarks import COUNTRY_BENCHMARKS
    best = _country_options(session, brand_id, budget_pkr, days, niche, price)
    orders, code, source = best[0]
    name = COUNTRY_BENCHMARKS[code]["name"]
    others = ", ".join(f"{COUNTRY_BENCHMARKS[c]['name']} ~{o:.0f} ({s.split(',')[0]})" for o, c, s in best[1:3])
    return code, f"{name}: about {orders:.0f} orders expected for this budget ({source}); {others or 'no other market'}"


SMALL_DAILY_USD = 25         # below this a day, one product gets the money instead of a whole catalogue
STRONG_SHARE = 0.35           # one product with this share of the store's real sales is its hero


def _facts(budget_pkr: float, days: int, items: list[dict], sets: list[dict], markets: list, niche: str) -> dict:
    """Everything a marketing manager would look at, as plain numbers."""
    from datetime import date, timedelta

    from .benchmarks import COUNTRY_BENCHMARKS, get_country
    from .fx import pkr_per_usd
    from .seasonality import events
    total_units = sum(int(i["meta"].get("store_units") or 0) for i in items)
    daily = budget_pkr / pkr_per_usd() / max(days, 1)
    top = sorted(items, key=lambda i: (-int(i["meta"].get("store_units") or 0), -i["score"]))[:8]
    start = date.today() + timedelta(days=1)
    season = []
    for _, code, _ in markets[:2]:
        for e in events(code, start, start + timedelta(days=days), niche)[:4]:
            season.append({"country": code, "date": e["date"], "event": e["name"], "note": e["note"]})
    return {
        "budget": {"total_pkr": budget_pkr, "days": days, "daily_usd": round(daily, 2),
                   "meta_min_daily_per_ad_set_usd": get_country(markets[0][1])["min_daily_usd"]},
        "store_sales": {"window": next((i["meta"].get("store_window") for i in items if i["meta"].get("store_window")), ""),
                        "units_total": total_units},
        "products": [{"key": i["key"], "title": i["title"], "category": i.get("site_category") or i.get("kind") or "",
                      "price_usd": i.get("price_usd") or i.get("site_price") or 0,
                      "margin_usd": round(i["price_usd"] - i["cost_usd"] - i["shipping_usd"], 2)
                      if i.get("price_usd") and (i.get("cost_usd") or i.get("shipping_usd")) else None,
                      "store_units_sold": int(i["meta"].get("store_units") or 0),
                      "share_of_store_sales_pct": round(100 * int(i["meta"].get("store_units") or 0) / total_units, 1)
                      if total_units else 0,
                      "meta_ad_spend_usd": i["meta"].get("spend_usd", 0), "meta_ad_sales": i["meta"].get("purchases", 0),
                      "meta_ad_clicks": i["meta"].get("clicks", 0)} for i in top],
        "catalogue_sets": [{"id": st["id"], "name": st["name"], "products": st["product_count"],
                            "ad_spend_usd": st["spend_usd"], "ad_sales": st["purchases"], "ad_clicks": st["clicks"],
                            "campaigns": st["campaigns"][:3]} for st in sets],
        "markets": [{"country": c, "name": COUNTRY_BENCHMARKS[c]["name"], "orders_expected": round(o, 1), "source": src}
                    for o, c, src in markets],
        "season": season,
    }


MANAGER_SCHEMA = {
    "type": "object",
    "properties": {
        "advertise": {"type": "string"},
        "country": {"type": "string"},
        "why_what": {"type": "string"},
        "why_country": {"type": "string"},
        "strategy": {"type": "string"},
        "other_ideas": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["advertise", "country", "why_what", "why_country", "strategy", "other_ideas"],
    "additionalProperties": False,
}

MANAGER_SYSTEM = """You are the marketing manager of an online store that sells to the US and the UK. The owner
gives you a budget and a number of days; you decide what to advertise on Meta and where, to get the most
profitable sales. Think like a manager, using only the numbers given:
- What buyers really buy (store_units_sold, share_of_store_sales_pct) is the strongest signal. Meta ad sales
  are a second signal; a catalogue's ad sales add up many products, so do not compare them one to one with a
  single product's ad sales.
- One clear best seller and a small daily budget: put the money behind that hero product.
- Sales spread over many products, or a budget big enough for Meta to learn across products: the catalogue.
- Never choose a product without stock or a margin at or below zero.
- When budget has owner_max_marketing_cost_per_order_pkr, prefer the option most likely to sell within it, and
  say in strategy how to stay within it; never promise it will be met.
- Markets: prefer the store's own results over benchmarks; mind the season and holidays in the window.
- advertise: exactly "catalogue:<id of a catalogue_sets item>" or "product:<key of a products item>".
- country: one code from markets.
- why_what, why_country: one or two plain sentences an owner who is new to marketing understands, with the
  numbers that decided it. strategy: two sentences on how you would run these days (test, then scale).
- other_ideas: up to 3 other moves worth considering beyond this campaign, each one plain sentence with the
  number behind it (a collection of the store to push, a bundle, a retargeting push, an offer for a holiday in
  the window...). Only ideas the facts support; an empty list is fine.
- Never write ids or keys in why_what, why_country, strategy or other_ideas: use names the owner knows."""


def _limit_view(session: Session, brand_id: int, budget_pkr: float, days: int, country: str, niche: str,
                price: float, limit_pkr: float) -> str:
    """Honest view of the owner's cost limit against what the numbers expect."""
    from .estimator import estimate_orders
    from .history import account_overrides
    from .outcomes import calibration_factor
    ov = account_overrides(country, niche, price, brand_id)
    cal = (1.0, 0) if ov else calibration_factor(session, country, brand_id, niche)
    e = estimate_orders(budget_pkr, country, niche, price, days, overrides=ov, calibration=cal)
    needed = int(budget_pkr // limit_pkr)
    base = f"To spend the whole budget within your limit needs about {needed} orders. "
    if not e.cpa_pkr:
        return base + "There is not enough data yet to say what one order will cost."
    if e.cpa_pkr <= limit_pkr:
        return base + f"The estimate is about PKR {e.cpa_pkr:,.0f} per order, within your limit."
    return base + (f"Warning: the estimate is about PKR {e.cpa_pkr:,.0f} per order, above your limit. Meta is told to "
                   "keep to your limit, so it may spend only part of the budget and bring fewer orders.")


def _based_on(facts: dict) -> list[str]:
    """The numbers this plan was decided on, one plain line each."""
    out = []
    b = facts["budget"]
    out.append(f"Budget: PKR {b['total_pkr']:,.0f} over {b['days']} days, about ${b['daily_usd']:.0f} a day.")
    if limit := b.get("owner_max_marketing_cost_per_order_pkr"):
        out.append(f"Your limit: at most PKR {limit:,} of ads for one order.")
    total = facts["store_sales"]["units_total"]
    sold = [p for p in facts["products"] if p["store_units_sold"]]
    if total:
        top = ", ".join(f"{p['title']} {p['store_units_sold']} ({p['share_of_store_sales_pct']:.0f}%)" for p in sold[:3])
        out.append(f"Your store's real sales ({facts['store_sales']['window'] or 'recent'}): {total} items; most sold: {top}.")
    else:
        out.append("Your store's real sales: not read yet (add the store keys and press Read products).")
    ads = [p for p in facts["products"] if p["meta_ad_sales"]]
    if ads:
        out.append("Products that sold through your Meta ads: " + ", ".join(
            f"{p['title']} {p['meta_ad_sales']} from ${p['meta_ad_spend_usd']:.0f}" for p in ads[:3]) + ".")
    if facts["catalogue_sets"]:
        cs = facts["catalogue_sets"][0]
        out.append(f"Your catalogue ads: best set '{cs['name']}' ({cs['products']} products), {cs['ad_sales']} sales "
                   f"from ${cs['ad_spend_usd']:.0f}.")
    if facts["markets"]:
        out.append("Markets: " + ", ".join(f"{m['name']} about {m['orders_expected']:.0f} orders ({m['source'].split(',')[0]})"
                                           for m in facts["markets"][:3]) + ".")
    for e in facts["season"][:2]:
        out.append(f"Season: {e['event']} on {e['date']} ({e['country']}).")
    return out


def _manager(facts: dict, sets: list[dict], items: list[dict], markets: list) -> dict | None:
    """The AI marketing manager's choice, checked against the real options; None when it cannot decide."""
    import json

    from . import llm
    from .config import PLAN_MODEL
    if not llm.configured():
        return None
    try:
        out = llm.structured(MANAGER_SYSTEM, "FACTS\n" + json.dumps(facts, indent=1), MANAGER_SCHEMA,
                             "Auto plan manager", effort="high", max_tokens=3000, model=PLAN_MODEL)
    except Exception:
        return None
    kind, _, ref = str(out.get("advertise", "")).partition(":")
    pick = {"country": str(out.get("country", "")).upper(), "why_what": str(out.get("why_what", "")).strip()[:600],
            "why_country": str(out.get("why_country", "")).strip()[:400],
            "strategy": str(out.get("strategy", "")).strip()[:500], "by": "AI marketing manager",
            "ideas": [str(x).strip()[:300] for x in (out.get("other_ideas") or []) if str(x).strip()][:3]}
    if pick["country"] not in {c for _, c, _ in markets} or not pick["why_what"]:
        return None
    if kind == "catalogue":
        st = next((x for x in sets if x["id"] == ref.strip()), None)
        return {**pick, "scope": "catalogue", "set": st} if st else None
    if kind == "product":
        it = next((x for x in items if x["key"] == ref.strip()), None)
        if it and not it["verdict"].startswith(("Loses money", "Out of stock")):
            return {**pick, "scope": "product", "item": it}
    return None


def _rule(facts: dict, sets: list[dict], items: list[dict], markets: list) -> dict:
    """Without the AI: a clear best seller (by real sales) or a small budget favours one product; otherwise
    the catalogue that sold best."""
    from .benchmarks import COUNTRY_BENCHMARKS
    total = facts["store_sales"]["units_total"]
    sellable = [i for i in items if not i["verdict"].startswith(("Loses money", "Out of stock"))]
    hero = max(sellable, key=lambda i: (int(i["meta"].get("store_units") or 0), i["score"]), default=None)
    units = int(hero["meta"].get("store_units") or 0) if hero else 0
    share = units / total if total else 0
    small = facts["budget"]["daily_usd"] < SMALL_DAILY_USD
    best_set = next((x for x in sets if x["purchases"] or x["clicks"] >= 50), None) or (sets[0] if sets else None)
    orders, code, source = markets[0]
    where = {"country": code, "why_country": f"{COUNTRY_BENCHMARKS[code]['name']}: about {orders:.0f} orders expected "
                                             f"for this budget ({source}).", "strategy": "", "by": "The app's rules"}
    if hero and (not best_set or (units >= 5 and share >= STRONG_SHARE) or (small and units >= 3)):
        reason = (f"Your clear best seller: {units} of your {total} sales ({share:.0%}). " if units else hero["reason"] + " ")
        reason += "With this budget one product gets enough money to sell." if small else \
            "Putting the budget behind it is the surest way to sales."
        return {**where, "scope": "product", "item": hero, "why_what": reason}
    # a product whose own ads sold more than the whole catalogue's did
    ad_hero = max(sellable, key=lambda i: (int(i["meta"].get("purchases") or 0), i["score"]), default=None)
    if ad_hero and best_set and int(ad_hero["meta"].get("purchases") or 0) > best_set["purchases"]:
        return {**where, "scope": "product", "item": ad_hero,
                "why_what": f"{ad_hero['reason']} Its own ads sold more than your whole catalogue's did "
                            f"({best_set['purchases']}), so it gets the budget."}
    if best_set:
        sold = (f"{best_set['purchases']} sales from ${best_set['spend_usd']:.0f} of catalogue ads"
                if best_set["purchases"] else f"{best_set['clicks']} clicks from your catalogue ads")
        return {**where, "scope": "catalogue", "set": best_set,
                "why_what": f"Your sales are spread over many products, and the catalogue already brought {sold}. "
                            "Catalogue ads show each person the products they are most likely to buy."}
    return {**where, "scope": "product", "item": sellable[0] if sellable else items[0],
            "why_what": (sellable[0] if sellable else items[0])["reason"]}


def decide(session: Session, brand_id: int, budget_pkr: float, days: int, max_cpa_pkr: float | None = None) -> dict:
    """max_cpa_pkr: the most the owner will pay in ads for one order (PKR); Meta and the alerts hold to it."""
    from . import brands, catalog, products
    from .meta_client import MetaError
    vals = brands.settings_for(session, brand_id)
    if not vals.get("meta_access_token") or not vals.get("meta_ad_account_id"):
        raise AutoPlanError("Connect this store to Meta first (Connections), so the app can read its results.")
    decisions: list[dict] = []

    def why(topic: str, choice: str, reason: str):
        decisions.append({"topic": topic, "choice": choice, "why": reason})

    # category: what the store sells
    cat = products.store_category(session, brand_id) or products.store_default(session, brand_id)
    niche, note = cat if cat else ("other", "")
    if niche == "other" and len(note) < 3:
        note = brands.main_name(session) if not brand_id else "the store's products"

    # every option on the table: catalogue product sets and the store's products, with their numbers
    try:
        cat_info = catalog.product_sets(vals)
    except MetaError as ex:
        cat_info = {"sets": [], "readable": False, "note": f"The catalogue could not be read: {ex}"}
    if cat_info["readable"]:  # an empty product set has nothing to show
        cat_info["sets"] = [s for s in cat_info["sets"] if s["product_count"] > 0]
    sets = cat_info["sets"] if cat_info["readable"] else []      # a catalogue campaign needs a readable catalogue
    items = [i for i in products.ranked(session, brand_id) if i["in_stock"]]
    if not sets and not items:
        raise AutoPlanError("Not enough to decide on yet: no catalogue the app can use and no products read. "
                            "Press Read products on the Products page (store keys), or add catalog_management to the token.")

    store_price, _ = _price(session, brand_id, items, niche)
    markets = _country_options(session, brand_id, budget_pkr, days, niche, store_price)
    facts = _facts(budget_pkr, days, items, sets, markets, niche)
    if max_cpa_pkr:
        facts["budget"]["owner_max_marketing_cost_per_order_pkr"] = round(max_cpa_pkr)

    choice = _manager(facts, sets, items, markets) or _rule(facts, sets, items, markets)
    extra_info = {"based_on": _based_on(facts), "ideas": choice.get("ideas") or []}
    scope, extra = choice["scope"], {}
    if scope == "catalogue":
        st = choice["set"]
        extra = {"scope": "catalogue", "product_set_id": st["id"], "product_set_name": st["name"],
                 "catalog_id": st["catalog_id"]}
        product = f"{note or 'Store'} catalogue: {st['name']}"[:200]
        landing = vals.get("store_url", "")
        why("What to advertise", f"Your catalogue: {st['name']} ({st['product_count']} products)", choice["why_what"])
    else:
        it = choice["item"]
        product, landing = it["title"], it["url"]
        if it.get("niche"):
            niche, note = it["niche"], it.get("category_note") or note
        why("What to advertise", it["title"], choice["why_what"])
    why("Category", note or niche, "From what the store sells (its own categories and products).")

    price, price_why = store_price, "your average order"
    if scope == "product" and choice["item"].get("price_usd"):
        price, price_why = float(choice["item"]["price_usd"]), "the product's own price"
    why("Price used for the estimate", f"${price:.2f}", price_why)

    country = choice["country"]
    why("Where", country, choice["why_country"])
    if max_cpa_pkr:
        why("Your cost limit", f"PKR {max_cpa_pkr:,.0f} of ads per order at most",
            _limit_view(session, brand_id, budget_pkr, days, country, niche, price, max_cpa_pkr))
        extra_info["max_cpa_pkr"] = round(float(max_cpa_pkr))
    if choice.get("strategy"):
        why("How to run it", "The manager's view", choice["strategy"])
    why("Decided by", choice["by"], "An AI marketing manager weighed all the numbers above."
        if choice["by"] == "AI marketing manager" else "The AI was not available, so the app's own rules decided.")

    # the sales brief, from the store's own ads and page (never invented)
    brief = {"offer": "", "usp": "", "delivery_days": 0, "returns": "", "social_proof": ""}
    try:
        from .brief_suggest import suggest
        got = suggest(session, brand_id, landing)
        brief.update({k: got.get(k) or brief[k] for k in brief})
        if got.get("what_your_ads_do"):
            why("Selling points", "From your own ads", got["what_your_ads_do"] +
                (" Check the offer is still running: it may come from an old sale." if brief["offer"] else ""))
    except Exception as ex:  # no AI key, no ads to read: the plan still works without a brief
        why("Selling points", "None filled", f"Your ads could not be read for selling points ({ex}). "
                                              "Add them under Make it sell when you edit the plan.")

    inputs = {"budget_pkr": budget_pkr, "country": country, "product": product, "niche": niche,
              "category_note": note if niche == "other" else (note or ""), "price_usd": price,
              "duration_days": days, "brand_id": brand_id, "landing_url": landing, **brief}
    return {"inputs": inputs, "extra": {**extra, **extra_info, "decisions": decisions}, "scope": scope,
            "notes": [n for n in (cat_info.get("note"),) if n]}
