"""Season and sales calendar for jackets / outerwear.

Two effects over the campaign's days:
- cpm: Meta auction prices. Q4 is expensive (every retailer bids), January is cheap.
- cvr: how many visitors buy a jacket. Highest from October to January in the northern hemisphere,
  lowest in early summer; flipped for the southern hemisphere; damped in the Gulf (little winter).
Factors are relative to the yearly average and blended halfway toward 1.0, because benchmarks and
account history already contain part of the season. Sales events add their own lift and appear as
notes for the planner.
"""

from __future__ import annotations

from datetime import date, timedelta

# Relative to the yearly average (northern hemisphere), index 0 = January.
_CPM = [0.85, 0.90, 0.95, 0.95, 0.95, 0.95, 0.95, 0.95, 1.00, 1.05, 1.25, 1.30]
_DEMAND = [1.00, 0.90, 0.80, 0.70, 0.60, 0.55, 0.55, 0.70, 0.95, 1.15, 1.30, 1.20]
_MEAN_DEMAND = sum(_DEMAND) / 12

SOUTH = {"AU"}
MILD = {"AE", "SA"}          # little winter: weaker jacket season
CHRISTMAS = {"US", "GB", "CA", "AU", "DE", "FR"}
BOXING_DAY = {"GB", "CA", "AU"}
INTL_SHIPPING_DAYS = 14      # Pakistan to most markets, door to door


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def events(country: str, start: date, end: date) -> list[dict]:
    """Sales moments that fall inside [start, end] for this country."""
    c = country.upper()
    out = []
    for y in {start.year, end.year}:
        bf = _nth_weekday(y, 11, 3, 4) + timedelta(days=1)  # day after the 4th Thursday
        cands = [
            (bf, "Black Friday", "Highest-intent days of the year; CPMs rise too. Have offers and creatives ready a week before", 1.30, 1.35),
            (bf + timedelta(days=3), "Cyber Monday", "Second peak of the Black Friday weekend", 1.20, 1.25),
            (date(y, 2, 14), "Valentine's Day", "Gift angle works for jackets; push 10 days before", 1.05, 1.10),
        ]
        if c in CHRISTMAS:
            cutoff = date(y, 12, 25) - timedelta(days=INTL_SHIPPING_DAYS + 1)
            cands.append((cutoff, "Last order day for Christmas delivery",
                          f"About {INTL_SHIPPING_DAYS} days shipping from Pakistan: after this, gift buyers drop off. Say the cutoff in ads", 1.15, 1.10))
            cands.append((date(y, 12, 25), "Christmas", "Gift demand ends at the shipping cutoff; self-purchase continues", 1.0, 1.0))
        if c in BOXING_DAY:
            cands.append((date(y, 12, 26), "Boxing Day sales", "Discount-driven buyers; a sale price converts best", 1.15, 1.20))
        if c in {"US", "GB", "CA"}:
            cands.append((_nth_weekday(y, 6, 6, 3), "Father's Day", "Gift angle for men's jackets; push 2 weeks before", 1.05, 1.10))
        if c == "AU":
            cands.append((_nth_weekday(y, 9, 6, 1), "Father's Day", "Gift angle for men's jackets; push 2 weeks before", 1.05, 1.10))
        for d, name, note, cpm, cvr in cands:
            if start <= d <= end:
                out.append({"date": d.isoformat(), "name": name, "note": note, "cpm": cpm, "cvr": cvr})
    return sorted(out, key=lambda e: e["date"])


def _month_factors(country: str, month: int) -> tuple[float, float]:
    c = country.upper()
    m = (month - 1 + (6 if c in SOUTH else 0)) % 12
    cpm = 1 + 0.5 * (_CPM[(month - 1) % 12] - 1)  # auction prices follow the calendar, not the weather
    demand = _DEMAND[m] / _MEAN_DEMAND
    weight = 0.25 if c in MILD else 0.5
    return cpm, 1 + weight * (demand - 1)


def season_factors(country: str, start: date, days: int) -> dict:
    """Average cpm / cvr factors over the campaign days, plus the events inside them."""
    days = max(days, 1)
    end = start + timedelta(days=days - 1)
    evs = events(country, start, end)
    ev_by_day = {e["date"]: e for e in evs}
    cpm_sum = cvr_sum = 0.0
    for i in range(days):
        d = start + timedelta(days=i)
        cpm, cvr = _month_factors(country, d.month)
        e = ev_by_day.get(d.isoformat())
        if e:  # the event day itself
            cpm, cvr = cpm * e["cpm"], cvr * e["cvr"]
        cpm_sum += cpm
        cvr_sum += cvr
    cpm_f, cvr_f = cpm_sum / days, cvr_sum / days
    if cvr_f >= 1.1:
        label = "peak season"
    elif cvr_f >= 0.97:
        label = "normal season"
    else:
        label = "off season"
    return {"cpm": round(cpm_f, 3), "cvr": round(cvr_f, 3), "label": label,
            "start": start.isoformat(), "end": end.isoformat(), "events": evs}
