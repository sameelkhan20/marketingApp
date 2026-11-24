"""Season and sales calendar, per product category.

Two effects over the campaign's days:
- cpm: Meta auction prices. Q4 is expensive (every retailer bids), January is cheap. Same for every
  product: it follows the calendar.
- cvr: how many visitors buy. Each category has a demand season (backend/benchmarks.py):
    winter  - jackets, outerwear: high Oct-Jan, low in early summer
    summer  - swimwear, summer wear: high Apr-Aug
    gifting - jewelry, beauty, gadgets, toys: Q4 and gift days
    steady  - most everyday products: a mild Q4 lift
  Weather-driven seasons are flipped for the southern hemisphere and damped where winter is mild.
Factors are relative to the yearly average and blended halfway toward 1.0, because benchmarks and
account history already contain part of the season. Sales events add their own lift, but only for
categories whose buyers care about them (a Mother's Day lift for cutlery and perfume, not for
biker jackets), and appear as notes for the planner.
"""

from __future__ import annotations

from datetime import date, timedelta

from .benchmarks import get_category

# Relative to the yearly average (northern hemisphere), index 0 = January.
_CPM = [0.85, 0.90, 0.95, 0.95, 0.95, 0.95, 0.95, 0.95, 1.00, 1.05, 1.25, 1.30]
_DEMAND = {
    "winter":  [1.00, 0.90, 0.80, 0.70, 0.60, 0.55, 0.55, 0.70, 0.95, 1.15, 1.30, 1.20],
    "summer":  [0.70, 0.75, 0.90, 1.05, 1.25, 1.35, 1.35, 1.20, 0.95, 0.80, 0.80, 0.90],
    "gifting": [0.85, 1.00, 0.90, 0.90, 1.00, 0.90, 0.85, 0.85, 0.90, 1.00, 1.30, 1.50],
    "steady":  [0.95, 0.95, 1.00, 1.00, 1.00, 0.95, 0.95, 1.00, 1.00, 1.00, 1.10, 1.15],
}
WEATHER = {"winter", "summer"}  # seasons that follow the weather (flipped south of the equator)

SOUTH = {"AU"}
MILD = {"AE", "SA"}  # little winter: a weaker coat season
CHRISTMAS = {"US", "GB", "CA", "AU", "DE", "FR"}
BOXING_DAY = {"GB", "CA", "AU"}
MUSLIM_MARKETS = {"AE", "SA", "PK"}
WHITE_FRIDAY = {"AE", "SA", "PK"}  # the name used in the Gulf and in Pakistan
INTL_SHIPPING_DAYS = 14      # Pakistan to most markets, door to door

# Eid dates (Umm al-Qura calendar; the moon can move them by a day).
EID_AL_FITR = {2026: date(2026, 3, 20), 2027: date(2027, 3, 10), 2028: date(2028, 2, 27), 2029: date(2029, 2, 14),
               2030: date(2030, 2, 4), 2031: date(2031, 1, 24)}
EID_AL_ADHA = {2026: date(2026, 5, 27), 2027: date(2027, 5, 17), 2028: date(2028, 5, 5), 2029: date(2029, 4, 24),
               2030: date(2030, 4, 13), 2031: date(2031, 4, 2)}


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _easter(year: int) -> date:
    """Western Easter Sunday (anonymous Gregorian algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    month = (h + l_ - 7 * m + 114) // 31
    day = ((h + l_ - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def shipping_days(country: str, delivery_days: int | None = None) -> int:
    """The store's own delivery time when the brief gives one; else ~14 days abroad, ~3 inside Pakistan."""
    if delivery_days and delivery_days > 0:
        return int(delivery_days)
    return 3 if country.upper() == "PK" else INTL_SHIPPING_DAYS


def events(country: str, start: date, end: date, category: str | None = None, delivery_days: int | None = None) -> list[dict]:
    """Sales moments inside [start, end] for this country. With a category, only the events its
    buyers care about, with lifts sized for it; without, every event at its full lift."""
    c = country.upper()
    cat = get_category(category) if category else None
    tags = cat["tags"] if cat else {"gift", "men", "women", "eid", "home"}
    noun = cat["noun"] if cat else "product"
    ship = shipping_days(c, delivery_days)
    out = []
    for y in range(start.year - 1, end.year + 1):  # a multi-day event can start the year before
        bf = _nth_weekday(y, 11, 3, 4) + timedelta(days=1)  # day after the 4th Thursday
        bf_name = "White Friday" if c in WHITE_FRIDAY else "Black Friday"
        # (date, name, note, cpm lift, cvr lift, tag needed or None for everyone[, days it lasts])
        cands = [
            (bf, bf_name, "Highest-intent days of the year; CPMs rise too. Have offers and creatives ready a week before", 1.30, 1.35, None),
            (bf + timedelta(days=3), "Cyber Monday", "Second peak of the Black Friday weekend", 1.20, 1.25, None),
            (date(y, 2, 14), "Valentine's Day", "Gift angle: push 10 days before, say the delivery date", 1.05, 1.10, "gift"),
        ]
        if c in CHRISTMAS:
            cutoff = date(y, 12, 25) - timedelta(days=ship + 1)
            cands.append((cutoff, "Last order day for Christmas delivery",
                          f"About {ship} days delivery: after this, gift buyers drop off. Say the cutoff in ads",
                          1.15, 1.10, "gift"))
            cands.append((date(y, 12, 25), "Christmas", "Gift demand ends at the shipping cutoff; self-purchase continues", 1.0, 1.0, None))
        if c in BOXING_DAY:
            cands.append((date(y, 12, 26), "Boxing Day sales", "Discount-driven buyers; a sale price converts best", 1.15, 1.20, None))
        if c in {"US", "GB", "CA"}:
            cands.append((_nth_weekday(y, 6, 6, 3), "Father's Day", f"Gift angle for a {noun} for him; push 2 weeks before", 1.05, 1.10, "men"))
        if c == "AU":
            cands.append((_nth_weekday(y, 9, 6, 1), "Father's Day", f"Gift angle for a {noun} for him; push 2 weeks before", 1.05, 1.10, "men"))
        if c in {"US", "CA", "AU", "PK", "IN"}:
            cands.append((_nth_weekday(y, 5, 6, 2), "Mother's Day", f"Gift angle for a {noun} for her; push 10 days before", 1.05, 1.15, "women"))
        if c == "GB":
            cands.append((_easter(y) - timedelta(days=21), "Mother's Day (Mothering Sunday)",
                          f"Gift angle for a {noun} for her; push 10 days before", 1.05, 1.15, "women"))
        if c in MUSLIM_MARKETS:
            if y in EID_AL_FITR:
                last = EID_AL_FITR[y] - timedelta(days=ship)
                cands.append((last - timedelta(days=10), "Last 10 days of Ramadan",
                              f"Peak Eid shopping. With about {ship} days delivery, orders after {last.isoformat()} arrive "
                              "after Eid: say the last order date in ads", 1.10, 1.25, "eid", 10))
                cands.append((EID_AL_FITR[y], "Eid al-Fitr", "Gifts and new outfits; deliveries must land before this day", 1.05, 1.10, "eid"))
            if y in EID_AL_ADHA:
                cands.append((EID_AL_ADHA[y] - timedelta(days=7 + ship), "Week before Eid al-Adha",
                              "Hosting and family gatherings: kitchen, dining and home buys peak", 1.05, 1.20, "home", 7))
        for cand in cands:
            d, name, note, cpm, cvr, need = cand[:6]
            span = cand[6] if len(cand) > 6 else 1
            if d + timedelta(days=span - 1) < start or d > end:
                continue  # no day of the event falls in the campaign
            if need and need not in tags:
                continue  # these buyers do not shop for this moment
            out.append({"date": d.isoformat(), "name": name, "note": note, "cpm": cpm, "cvr": cvr, "days": span})
    return sorted(out, key=lambda e: e["date"])


def _month_factors(country: str, month: int, season: str) -> tuple[float, float]:
    c = country.upper()
    curve = _DEMAND.get(season, _DEMAND["steady"])
    shift = 6 if (c in SOUTH and season in WEATHER) else 0
    m = (month - 1 + shift) % 12
    cpm = 1 + 0.5 * (_CPM[(month - 1) % 12] - 1)  # auction prices follow the calendar, not the weather
    demand = curve[m] / (sum(curve) / 12)
    weight = 0.25 if (c in MILD and season == "winter") else 0.5
    return cpm, 1 + weight * (demand - 1)


def season_factors(country: str, start: date, days: int, category: str | None = None,
                   delivery_days: int | None = None) -> dict:
    """Average cpm / cvr factors over the campaign days, plus the events inside them."""
    days = max(days, 1)
    end = start + timedelta(days=days - 1)
    season = get_category(category)["season"] if category else "winter"
    evs = events(country, start, end, category, delivery_days)
    ev_by_day: dict[str, dict] = {}
    for e in evs:  # an event lifts every one of its days
        first = date.fromisoformat(e["date"])
        for k in range(e.get("days", 1)):
            ev_by_day.setdefault((first + timedelta(days=k)).isoformat(), e)
    cpm_sum = cvr_sum = 0.0
    for i in range(days):
        d = start + timedelta(days=i)
        cpm, cvr = _month_factors(country, d.month, season)
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
    return {"cpm": round(cpm_f, 3), "cvr": round(cvr_f, 3), "label": label, "kind": season,
            "start": start.isoformat(), "end": end.isoformat(), "events": evs}
