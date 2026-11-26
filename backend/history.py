"""Account history override for the estimator.

After a Meta sync, account_stats holds real CPM / CTR / CVR per country.
If the target country has data, it replaces the benchmark. With few purchases
only CPM and CTR are trusted; CVR stays on the benchmark (blended).
"""

from .benchmarks import get_country, get_niche, price_multiplier
from .db import get_session, stat_for


# Categories whose calibration keeps the plain country key (see outcomes.cal_key): the main store sold
# jackets before categories existed, and their learned corrections carry on under that key.
HISTORY_NICHES = {"leather_jackets", "celebrity_jackets"}


def account_overrides(country: str, niche: str, price_usd: float = 120.0, brand_id: int = 0) -> dict | None:
    """Each store's plans launch on that store's own ad account, so that account's synced history replaces
    the benchmarks for the store's plans, whatever the category. A store that has not synced yet (or has
    too little data in this country) uses benchmarks."""
    with get_session() as s:
        st = stat_for(s, int(brand_id or 0), country)
        if not st or st.impressions < 10_000:
            return None

        bench = get_country(country)
        n = get_niche(niche)
        typ = n.get("typical_price", 150)
        bench_cvr = bench["cvr"] * n["cvr"] * price_multiplier(price_usd, typ)

        # The account's CVR comes from its own average order: scale it for a pricier or cheaper product.
        account_cvr = st.cvr
        if st.purchases > 0 and st.revenue_usd > 0:
            avg_price = st.revenue_usd / st.purchases
            account_cvr = st.cvr * price_multiplier(price_usd, typ) / max(price_multiplier(avg_price, typ), 1e-6)
        if st.purchases >= 20 and account_cvr > 0:
            cvr = account_cvr
        elif st.purchases > 0 and account_cvr > 0:
            cvr = (account_cvr + bench_cvr) / 2  # thin data: blend
        else:
            cvr = bench_cvr

        return {
            # a zero (no clicks recorded, broken sync) would make every estimate zero: use the benchmark
            "cpm_usd": st.cpm_usd if st.cpm_usd > 0 else bench["cpm_usd"],
            "ctr": st.ctr if st.ctr > 0 else bench["ctr"] * n.get("ctr", 1.0),
            "cvr": round(cvr, 4),
            "confidence": st.confidence,
            "source": f"account_history ({st.purchases} purchases, {st.since} to {st.until})",
        }
