"""Meta needs a minimum daily budget per ad set. A plan whose ad sets fall below it cannot launch, so
small ad sets are folded into the largest one (Retargeting first to go) and the shares are scaled to 100.
The planner, the launch checklist and the launcher all use this one function, so they always agree."""

from __future__ import annotations


def fit_ad_sets(ad_sets: list[dict], daily_total: float, min_per_ad_set: float) -> tuple[list[dict], list[str]]:
    """(ad sets that each meet the minimum, names that were folded). daily_total and min in one currency."""
    sets = [dict(a) for a in ad_sets if isinstance(a, dict)]
    if not sets:
        return sets, []
    folded: list[str] = []

    def shares():
        raw = [max(float(a.get("daily_budget_pct", 0) or 0), 0.0) for a in sets]
        tot = sum(raw)
        return [r / tot for r in raw] if tot > 0 else [1 / len(sets)] * len(sets)

    while len(sets) > 1:
        sh = shares()
        low = [i for i, s in enumerate(sh) if daily_total * s < min_per_ad_set]
        if not low:
            break
        # drop the smallest (a Retargeting ad set first): its share goes to the largest
        retarget = [i for i in low if "retarget" in str(sets[i].get("name", "")).lower()]
        i = retarget[0] if retarget else min(low, key=lambda k: sh[k])
        big = max((k for k in range(len(sets)) if k != i), key=lambda k: sh[k])
        sets[big]["daily_budget_pct"] = float(sets[big].get("daily_budget_pct", 0) or 0) + \
            float(sets[i].get("daily_budget_pct", 0) or 0)
        folded.append(str(sets[i].get("name", f"Ad set {i + 1}")))
        del sets[i]
    sh = shares()
    for a, s in zip(sets, sh):
        a["daily_budget_pct"] = round(s * 100, 2)
    return sets, folded
