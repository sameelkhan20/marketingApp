"""Benchmark data used when the account has no history yet.

Numbers are approximate Meta e-commerce (conversion objective) medians.
Once the Meta account is connected, backend/history.py overrides these with
the account's own 90-day / 2-year figures, which are far more accurate.

cpm_usd: cost per 1000 impressions
ctr: link click-through rate (0-1)
cvr: purchase conversion rate on landing page (0-1)
"""

COUNTRY_BENCHMARKS: dict[str, dict] = {
    "US": {"name": "United States", "cpm_usd": 14.0, "ctr": 0.012, "cvr": 0.020, "min_daily_usd": 5},
    "GB": {"name": "United Kingdom", "cpm_usd": 10.0, "ctr": 0.012, "cvr": 0.020, "min_daily_usd": 5},
    "CA": {"name": "Canada", "cpm_usd": 11.0, "ctr": 0.012, "cvr": 0.019, "min_daily_usd": 5},
    "AU": {"name": "Australia", "cpm_usd": 11.5, "ctr": 0.012, "cvr": 0.019, "min_daily_usd": 5},
    "DE": {"name": "Germany", "cpm_usd": 8.5, "ctr": 0.010, "cvr": 0.017, "min_daily_usd": 5},
    "FR": {"name": "France", "cpm_usd": 7.5, "ctr": 0.010, "cvr": 0.016, "min_daily_usd": 5},
    "AE": {"name": "UAE", "cpm_usd": 6.5, "ctr": 0.013, "cvr": 0.018, "min_daily_usd": 5},
    "SA": {"name": "Saudi Arabia", "cpm_usd": 5.5, "ctr": 0.013, "cvr": 0.017, "min_daily_usd": 5},
    "PK": {"name": "Pakistan", "cpm_usd": 0.9, "ctr": 0.015, "cvr": 0.012, "min_daily_usd": 1},
    "IN": {"name": "India", "cpm_usd": 1.2, "ctr": 0.014, "cvr": 0.011, "min_daily_usd": 1},
}

# The store sells jackets / upperwear. Two niches, each with its own
# multipliers on (cpm, ctr, cvr) relative to generic apparel e-commerce,
# plus guidance the planner uses for targeting and creatives.
NICHE_MULTIPLIERS: dict[str, dict] = {
    "leather_jackets": {
        "label": "Leather jackets (generic)",
        "cpm": 1.00, "ctr": 1.00, "cvr": 0.90,
        "guidance": (
            "Generic leather / biker / bomber / aviator jackets and upperwear. Buyers shop on "
            "style, material quality and price. Target: 22-45, interests like leather jackets, "
            "motorcycle apparel, streetwear, Zara/AllSaints/Schott shoppers; broad Advantage+ works "
            "well. Creatives: product-on-model video, close-ups of stitching/leather grain, "
            "size/fit confidence, free shipping. Peak season Sept-Jan in northern countries."
        ),
    },
    "celebrity_jackets": {
        "label": "Celebrity / movie-inspired jackets",
        "cpm": 1.05, "ctr": 1.25, "cvr": 0.85,
        "guidance": (
            "Jackets inspired by celebrities, films, TV series and games (e.g. a jacket seen on a "
            "character or star). Buyers are fans: target interests in the specific film/series/"
            "artist, cosplay, comic-cons, fan pages; strong hooks like 'the jacket from <show>'. "
            "Higher CTR but more impulse browsing, so lower CVR. Risk: Meta may reject ads using "
            "trademarked names, logos or celebrity likeness; describe the style ('inspired by') "
            "and never use official stills, logos or the actor's face in creatives. Time launches "
            "with releases/premieres."
        ),
    },
}
DEFAULT_NICHE = "leather_jackets"

# Price elasticity for jackets: higher ticket -> lower CVR.
# (price_usd band, cvr multiplier). Baseline is a ~$100-150 jacket.
PRICE_BANDS = [
    (60, 1.30),
    (100, 1.10),
    (150, 1.00),
    (220, 0.80),
    (350, 0.60),
    (10**9, 0.40),
]


def price_multiplier(price_usd: float) -> float:
    for ceiling, mult in PRICE_BANDS:
        if price_usd <= ceiling:
            return mult
    return 0.30


def get_country(code: str) -> dict:
    code = code.upper()
    if code not in COUNTRY_BENCHMARKS:
        raise KeyError(f"No benchmark for country '{code}'. Known: {', '.join(COUNTRY_BENCHMARKS)}")
    return COUNTRY_BENCHMARKS[code]


def get_niche(niche: str) -> dict:
    return NICHE_MULTIPLIERS.get(niche.lower(), NICHE_MULTIPLIERS[DEFAULT_NICHE])
