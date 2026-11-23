"""Account history override for the estimator.

After a Meta sync, account_stats holds real CPM / CTR / CVR per country.
If the target country has data, it replaces the benchmark. With few purchases
only CPM and CTR are trusted; CVR stays on the benchmark (blended).
"""

from .benchmarks import get_country, get_niche, price_multiplier
from .db import AccountStat, get_session


def account_overrides(country: str, niche: str, price_usd: float = 120.0) -> dict | None:
    with get_session() as s:
        st = s.get(AccountStat, country.upper())
        if not st or st.impressions < 10_000:
            return None

        bench = get_country(country)
        n = get_niche(niche)
        bench_cvr = bench["cvr"] * n["cvr"] * price_multiplier(price_usd)

        # The account's CVR comes from its own average order: scale it for a pricier or cheaper jacket.
        account_cvr = st.cvr
        if st.purchases > 0 and st.revenue_usd > 0:
            avg_price = st.revenue_usd / st.purchases
            account_cvr = st.cvr * price_multiplier(price_usd) / max(price_multiplier(avg_price), 1e-6)
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
