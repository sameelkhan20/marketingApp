"""Paid API usage and the monthly AI budget (same idea as the Social Agent's Billing page).

Every Claude call and every AI image is recorded with an ESTIMATED cost from the price table
below (editable on the Payments page). Providers bill from their own meters; their dashboards
are the source of truth. When this month's estimated AI cost reaches the budget, new AI calls
stop: plans fall back to the template, images are refused, alerts keep their built-in advice.
Meta ad spend is separate: it is read from the campaigns themselves (plan outcomes).
"""

import json
import logging
from datetime import datetime

from sqlalchemy import func, select

from .db import ApiUsage, Setting, get_session

log = logging.getLogger("usage")

# US dollars. "in"/"out": per 1 million tokens; "image": per image. "provider:*" covers every model.
DEFAULT_PRICES: dict[str, dict[str, float]] = {
    "anthropic:claude-opus-5-5": {"in": 4.0, "out": 20.0},
    "anthropic:claude-sonnet-5-5": {"in": 2.0, "out": 10.0},
    "anthropic:claude-haiku-5-5": {"in": 0.10, "out": 0.50},
    "anthropic:*": {"in": 4.0, "out": 20.0},
    "higgsfield:*": {"image": 0.0},  # paid with plan credits; set a per-image value to track it
    "gemini:gemini-3.1-flash-lite": {"in": 0.25, "out": 1.50},
    "gemini:*": {"in": 0.30, "out": 2.50, "image": 0.039},
    "openai:*": {"image": 0.04},
}
DEFAULT_BUDGET_USD = 25.0


class BudgetExceeded(RuntimeError):
    pass


def _get(key: str) -> str:
    with get_session() as s:
        row = s.get(Setting, key)
        return row.value if row else ""


def _put(key: str, value: str) -> None:
    with get_session() as s:
        row = s.get(Setting, key)
        if row:
            row.value = value
        else:
            s.add(Setting(key=key, value=value))
        s.commit()


def prices() -> dict[str, dict[str, float]]:
    try:
        custom = json.loads(_get("billing_prices") or "{}")
    except ValueError:
        custom = {}
    return {**DEFAULT_PRICES, **custom}


def set_prices(table: dict[str, dict[str, float]]) -> None:
    clean = {k: {f: max(0.0, float(v)) for f, v in rates.items() if f in ("in", "out", "image")}
             for k, rates in table.items() if ":" in k}
    _put("billing_prices", json.dumps(clean))


def budget() -> float:
    try:
        return float(_get("billing_budget_usd") or DEFAULT_BUDGET_USD)
    except ValueError:
        return DEFAULT_BUDGET_USD


def set_budget(usd: float) -> None:
    _put("billing_budget_usd", str(max(0.0, float(usd))))


def _rate(provider: str, model: str) -> dict[str, float]:
    table = prices()
    return table.get(f"{provider}:{model}") or table.get(f"{provider}:*") or {}


def cost(provider: str, model: str, input_tokens: int = 0, output_tokens: int = 0, images: int = 0) -> float:
    r = _rate(provider, model)
    return round(input_tokens / 1e6 * r.get("in", 0) + output_tokens / 1e6 * r.get("out", 0)
                 + images * r.get("image", 0), 6)


def month_start(now: datetime | None = None) -> datetime:
    now = now or datetime.utcnow()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def month_cost() -> float:
    with get_session() as s:
        return float(s.scalar(select(func.coalesce(func.sum(ApiUsage.cost_usd), 0))
                              .where(ApiUsage.created_at >= month_start())) or 0)


def ensure_budget(feature: str) -> None:
    """Call before every paid AI request."""
    b = budget()
    if b > 0 and month_cost() >= b:
        raise BudgetExceeded(f"This month's AI budget (${b:.2f}) is used up, so {feature} is paused. "
                             "Raise it on the Payments page.")


def record(provider: str, model: str, feature: str, input_tokens: int = 0, output_tokens: int = 0,
           images: int = 0, ok: bool = True, error: str = "") -> None:
    try:
        with get_session() as s:
            s.add(ApiUsage(provider=provider, model=model or "", feature=feature, input_tokens=input_tokens,
                           output_tokens=output_tokens, images=images,
                           cost_usd=cost(provider, model, input_tokens, output_tokens, images),
                           ok=1 if ok else 0, error=(error or "")[:500]))
            s.commit()
    except Exception as ex:  # recording must never break the real call
        log.warning("usage not recorded: %s", ex)


def record_claude(response, feature: str) -> None:
    """From an Anthropic SDK response (usage.input_tokens / output_tokens; cache reads count as input)."""
    u = getattr(response, "usage", None)
    if u is None:
        return
    inp = (getattr(u, "input_tokens", 0) or 0) + (getattr(u, "cache_read_input_tokens", 0) or 0) \
        + (getattr(u, "cache_creation_input_tokens", 0) or 0)
    record("anthropic", getattr(response, "model", "") or "", feature, inp, getattr(u, "output_tokens", 0) or 0)


def summary() -> dict:
    """Everything the Payments page shows."""
    start = month_start()
    with get_session() as s:
        rows = s.scalars(select(ApiUsage).where(ApiUsage.created_at >= start).order_by(ApiUsage.id.desc())).all()
        recent = s.scalars(select(ApiUsage).order_by(ApiUsage.id.desc()).limit(50)).all()
    by_feature: dict[str, dict] = {}
    by_day: dict[str, float] = {}
    for r in rows:
        f = by_feature.setdefault(r.feature, {"feature": r.feature, "calls": 0, "images": 0, "tokens": 0, "cost_usd": 0.0})
        f["calls"] += 1
        f["images"] += r.images
        f["tokens"] += r.input_tokens + r.output_tokens
        f["cost_usd"] += r.cost_usd
        day = r.created_at.date().isoformat()
        by_day[day] = by_day.get(day, 0) + r.cost_usd
    spent = sum(r.cost_usd for r in rows)
    b = budget()
    return {
        "month": start.strftime("%B %Y"),
        "spent_usd": round(spent, 4),
        "budget_usd": b,
        "budget_used_pct": round(spent / b * 100, 1) if b else 0,
        "calls": len(rows),
        "images": sum(r.images for r in rows),
        "failed": sum(1 for r in rows if not r.ok),
        "by_feature": sorted(({**v, "cost_usd": round(v["cost_usd"], 4)} for v in by_feature.values()),
                             key=lambda v: -v["cost_usd"]),
        "by_day": [{"day": d, "cost_usd": round(c, 4)} for d, c in sorted(by_day.items())],
        "recent": [{"created_at": r.created_at.isoformat(), "provider": r.provider, "model": r.model,
                    "feature": r.feature, "input_tokens": r.input_tokens, "output_tokens": r.output_tokens,
                    "images": r.images, "cost_usd": round(r.cost_usd, 4), "ok": bool(r.ok), "error": r.error}
                   for r in recent],
        "prices": prices(),
    }
