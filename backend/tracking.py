"""Pixel health. Meta optimises for the Purchase event: if the pixel stops firing, or fires page views
but no purchases, the campaign spends blind. Checked before launch and on every monitor run."""

from datetime import datetime, timezone


def _hours_since(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        t = datetime.fromisoformat(ts.replace("+0000", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - t).total_seconds() / 3600


def problems(health: dict, clicks_7d: int = 0) -> list[dict]:
    """[{severity, text}] for what is wrong with the pixel. clicks_7d: link clicks the campaign sent
    in the same window (a purchase gap only matters once enough people reached the store)."""
    out = []
    if health.get("unavailable"):
        out.append({"key": "unavailable", "severity": "critical", "text": "Meta reports the pixel as unavailable"})
        return out
    hours = _hours_since(health.get("last_fired_time"))
    if hours is None:
        out.append({"key": "never_fired", "severity": "critical", "text": "The pixel has never fired: no tracking on the store"})
        return out
    if hours > 48:
        out.append({"key": "stale", "severity": "critical", "text": f"The pixel has not fired for {int(hours)} hours"})
    counts = health.get("counts", {})
    views = counts.get("PageView", 0)
    if views and not counts.get("Purchase") and clicks_7d >= 100:
        out.append({"key": "no_purchase", "severity": "warning",
                    "text": f"{views} page views but no Purchase events in 7 days, after {clicks_7d} ad clicks. "
                            "Check the purchase event on the order-confirmation page"})
    if views >= 200 and not counts.get("AddToCart"):
        out.append({"key": "no_cart", "severity": "info", "text": f"{views} page views but no AddToCart events: the cart button may not be tracked"})
    return out


SUGGESTIONS = [
    {"action": "Open Events Manager, Test events, and place a test order to see which events arrive",
     "expected_effect": "Shows within minutes whether Purchase is tracked", "risk": "None"},
    {"action": "Re-install the pixel or the store's Meta integration, then check the event on the order-confirmation page",
     "expected_effect": "Meta can optimise for sales again within a day", "risk": "None"},
    {"action": "Until it is fixed, pause scaling and keep budgets flat",
     "expected_effect": "Avoids spending more while results are not measured", "risk": "Slower growth for a few days"},
]
