"""Order estimate from budget + country + product.

Chain: budget -> impressions (CPM) -> clicks (CTR) -> orders (CVR).
Returns a low/expected/high range plus the assumptions used, so the UI can
show *why* the number is what it is. Account history (when connected)
replaces benchmark inputs via the `overrides` argument.
"""

from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

from .benchmarks import get_country, get_niche, price_multiplier
from . import fx

# How much of the budget is burned on learning/testing before efficient delivery.
TESTING_WASTE = 0.15


@dataclass
class Estimate:
    budget_pkr: float
    budget_usd: float
    effective_usd: float
    cpm_usd: float
    ctr: float
    cvr: float
    impressions: int
    clicks: int
    orders_low: int
    orders_expected: int
    orders_high: int
    cpa_usd: float
    cpa_pkr: float
    daily_usd: float
    confidence: str
    source: str
    warnings: list[str]
    assumptions: dict
    # profit (0 / None when no cost was given)
    margin_usd: float = 0.0
    break_even_cpa_usd: float = 0.0
    profit_usd: float | None = None
    profit_low_usd: float | None = None
    profit_high_usd: float | None = None
    season: dict = field(default_factory=dict)


def estimate_orders(
    budget_pkr: float,
    country: str,
    niche: str = "general",
    price_usd: float = 40.0,
    duration_days: int = 30,
    overrides: dict | None = None,
    calibration: tuple[float, int] = (1.0, 0),
    unit_cost_usd: float = 0.0,
    shipping_usd: float = 0.0,
    start: date | None = None,
    delivery_days: int = 0,
    cod: bool = False,
    return_rate_pct: float = 0.0,
) -> Estimate:
    """calibration: (factor, samples) from past launches in this country; multiplies the order estimate."""
    c = get_country(country)
    n = get_niche(niche)
    warnings: list[str] = []

    cpm = c["cpm_usd"] * n["cpm"]
    ctr = c["ctr"] * n["ctr"]
    cvr = c["cvr"] * n["cvr"] * price_multiplier(price_usd, n.get("typical_price", 150))
    source = "benchmark"
    confidence = "low"

    # Account history wins over benchmarks when available.
    if overrides:
        cpm = overrides.get("cpm_usd", cpm)
        ctr = overrides.get("ctr", ctr)
        cvr = overrides.get("cvr", cvr)
        source = overrides.get("source", "account_history")
        confidence = overrides.get("confidence", "high")

    # Season: auction prices and this category's demand over the campaign's own days (it starts after launch).
    from .seasonality import season_factors
    season = season_factors(country, start or (date.today() + timedelta(days=1)), duration_days, niche, delivery_days)
    cpm *= season["cpm"]
    cvr *= season["cvr"]
    if season["label"] == "off season":
        warnings.append(f"Off season for {n['label'].lower()} in {c['name']}: fewer buyers than usual. "
                        "A shorter test or a stronger offer converts better now.")
    for e in season["events"]:
        warnings.append(f"{e['name']} on {e['date']} falls in this campaign. {e['note']}.")

    rate = fx.pkr_per_usd()  # today's rate; the plan keeps it in assumptions
    budget_usd = budget_pkr / rate
    effective = budget_usd * (1 - TESTING_WASTE)
    daily = budget_usd / max(duration_days, 1)

    if daily < c["min_daily_usd"] * 2:
        warnings.append(
            f"Daily budget ${daily:.2f} is very low for {c['name']}. "
            f"Meta needs about ${c['min_daily_usd'] * 2}/day per ad set to exit learning phase. "
            "Consider fewer days or fewer ad sets."
        )

    impressions = effective / cpm * 1000 if cpm > 0 else 0.0
    clicks = impressions * ctr
    orders = clicks * cvr

    cal_factor, cal_samples = calibration
    if cal_samples > 0 and cal_factor != 1.0:
        orders *= cal_factor
        if cal_samples >= 3 and confidence == "low":
            confidence = "medium"

    # Range: ±40% at low confidence, ±20% at high.
    spread = {"low": 0.45, "medium": 0.30, "high": 0.20}[confidence]
    low = int(orders * (1 - spread))
    high = int(round(orders * (1 + spread)))
    expected = int(round(orders))

    if expected < 5:
        warnings.append(
            "Fewer than 5 expected orders: Meta's algorithm needs ~50 conversions/week to optimise. "
            "Results will be noisy; a higher budget or a cheaper product improves accuracy."
        )

    cpa = budget_usd / orders if orders > 0 else 0.0  # the user pays the full budget, learning spend included
    if orders <= 0:
        warnings.append("No orders can be expected with these numbers: the cost per order cannot be estimated.")

    # Returned, refunded or refused orders: Meta counts them as purchases, but they bring no money.
    # Pakistan with cash on delivery and no rate given: assume 20% refused (typical for COD there).
    home = country.upper() == "PK"
    returns = max(0.0, min(float(return_rate_pct or 0), 80.0)) / 100
    assumed_returns = False
    if cod and home and returns == 0:
        returns, assumed_returns = 0.20, True
    if returns:
        how = "are assumed to be" if assumed_returns else "are"
        hint = " (cash on delivery: set your real rate under the profit figure)" if assumed_returns else ""
        tip = ("Confirming COD orders by phone or WhatsApp before dispatch cuts refusals" if home else
               "A size guide, real photos and the delivery time in the ad cut refunds")
        warnings.append(f"About {returns:.0%} of orders {how} refunded or returned{hint}: of ~{int(round(orders))} "
                        f"orders Meta reports, about {int(round(orders * (1 - returns)))} are paid. The targets allow "
                        f"for it. {tip}")

    # Profit per order Meta reports. Abroad (US, UK...): a refunded order loses the price, and the product and
    # shipping are spent (sending it back to Pakistan costs more than it is worth). In Pakistan a refused parcel
    # comes back: the product is kept, shipping is paid both ways. Break-even cost per reported order = margin.
    if home:
        margin_raw = (1 - returns) * (price_usd - unit_cost_usd) - shipping_usd * (1 + returns)
    else:
        margin_raw = (1 - returns) * price_usd - unit_cost_usd - shipping_usd
    margin = round(margin_raw, 2) if (unit_cost_usd or shipping_usd) else 0.0
    profit = profit_lo = profit_hi = None
    if unit_cost_usd or shipping_usd:
        # from the unrounded order counts: rounding 0.7 orders up to 1 must not turn a loss into a profit
        profit = round(orders * margin - budget_usd, 2)
        profit_lo = round(max(orders * (1 - spread), 0) * margin - budget_usd, 2)
        profit_hi = round(orders * (1 + spread) * margin - budget_usd, 2)
        if margin <= 0:
            warnings.append(f"Price ${price_usd:.2f} does not cover cost, shipping{' and returns' if returns else ''}: "
                            "every order loses money.")
        elif orders <= 0 or cpa > margin:
            warnings.append(f"At about ${cpa:.2f} per order this plan loses ${cpa - margin:.2f} on each order "
                            f"(you keep ${margin:.2f} after cost and shipping). Raise the price, cut cost, "
                            "or treat it as a test to find cheaper audiences.")

    return Estimate(
        budget_pkr=budget_pkr,
        budget_usd=round(budget_usd, 2),
        effective_usd=round(effective, 2),
        cpm_usd=round(cpm, 2),
        ctr=round(ctr, 4),
        cvr=round(cvr, 4),
        impressions=int(impressions),
        clicks=int(clicks),
        orders_low=max(low, 0),
        orders_expected=expected,
        orders_high=high,
        cpa_usd=round(cpa, 2),
        cpa_pkr=round(cpa * rate),
        daily_usd=round(daily, 2),
        confidence=confidence,
        source=source,
        warnings=warnings,
        margin_usd=margin,
        break_even_cpa_usd=margin if margin > 0 else 0.0,
        profit_usd=profit,
        profit_low_usd=profit_lo,
        profit_high_usd=profit_hi,
        season=season,
        assumptions={
            "pkr_per_usd": rate,
            "testing_waste_pct": TESTING_WASTE * 100,
            "country": c["name"],
            "niche": niche,
            "price_usd": price_usd,
            "duration_days": duration_days,
            "calibration_factor": cal_factor if cal_samples else None,
            "orders_raw": round(orders, 3),  # unrounded, after calibration (outcomes compare against this)
            "calibration_samples": cal_samples,
            "unit_cost_usd": unit_cost_usd,
            "shipping_usd": shipping_usd,
            "cod": bool(cod),
            "return_rate_pct": round(returns * 100, 1),
            "return_rate_assumed": assumed_returns,
        },
    )


def estimate_to_dict(e: Estimate) -> dict:
    return asdict(e)
