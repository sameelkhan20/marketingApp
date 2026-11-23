"""Live US dollar to Pakistani rupee rate.

The open exchange-rate feed (open.er-api.com, free, no key, updated daily) is read at most every
6 hours and kept in the settings table, so a restart or a network failure falls back to the last
rate that was fetched, and only then to PKR_PER_USD in .env.

New estimates use the live rate. Each plan keeps the rate it was estimated with
(estimate.assumptions.pkr_per_usd) and each launch the rate it was created with, so the guardrails
of a running campaign do not move when the rupee does.
"""

import json
import logging
import threading
import time
from datetime import datetime, timezone

import requests

from .config import FX_LIVE, PKR_PER_USD

log = logging.getLogger("fx")
URL = "https://open.er-api.com/v6/latest/USD"
MAX_AGE = 6 * 3600
RETRY_AFTER = 15 * 60  # after a failed fetch, do not try again on every request
_lock = threading.Lock()
_cache: dict = {}  # {"rate", "fetched_at" (unix), "source"}
_last_try = [0.0]


def _sane(rate) -> bool:
    try:
        return 50 <= float(rate) <= 2000
    except (TypeError, ValueError):
        return False


def _load_saved() -> dict:
    from .db import Setting, get_session
    try:
        with get_session() as s:
            row = s.get(Setting, "fx_pkr_per_usd")
            d = json.loads(row.value) if row and row.value else {}
        return d if _sane(d.get("rate")) else {}
    except Exception:
        return {}


def _save(d: dict) -> None:
    from .db import Setting, get_session
    try:
        with get_session() as s:
            row = s.get(Setting, "fx_pkr_per_usd")
            if row:
                row.value = json.dumps(d)
            else:
                s.add(Setting(key="fx_pkr_per_usd", value=json.dumps(d)))
            s.commit()
    except Exception as ex:  # the rate still works from memory
        log.warning("could not save the exchange rate: %s", ex)


def _fetch() -> dict | None:
    try:
        r = requests.get(URL, timeout=6)
        data = r.json()
        rate = (data.get("rates") or {}).get("PKR")
        if r.status_code == 200 and data.get("result") == "success" and _sane(rate):
            return {"rate": round(float(rate), 2), "fetched_at": time.time(), "source": "open.er-api.com"}
        log.warning("exchange rate feed answered without a usable PKR rate (%s)", r.status_code)
    except (requests.RequestException, ValueError) as ex:
        log.warning("exchange rate fetch failed: %s", ex)
    return None


def info() -> dict:
    """{"rate", "updated" (ISO or ""), "source": "live" | "saved" | "fixed"}"""
    if not FX_LIVE:
        return {"rate": PKR_PER_USD, "updated": "", "source": "fixed"}
    with _lock:
        if not _cache:
            _cache.update(_load_saved())
        fresh = _cache and time.time() - _cache.get("fetched_at", 0) < MAX_AGE
        if not fresh and time.time() - _last_try[0] > RETRY_AFTER:
            _last_try[0] = time.time()
            got = _fetch()
            if got:
                _cache.clear()
                _cache.update(got)
                _save(got)
                fresh = True
        if not _cache:
            return {"rate": PKR_PER_USD, "updated": "", "source": "fixed"}
        updated = datetime.fromtimestamp(_cache["fetched_at"], timezone.utc).isoformat()
        return {"rate": float(_cache["rate"]), "updated": updated, "source": "live" if fresh else "saved"}


def pkr_per_usd() -> float:
    """Today's rate, for new estimates and figures shown now."""
    return float(info()["rate"])


def plan_rate(plan) -> float:
    """The rate a plan was estimated with (stable for its whole life); today's rate for old plans."""
    try:
        r = float(((plan.estimate or {}).get("assumptions") or {}).get("pkr_per_usd") or 0)
    except (TypeError, ValueError, AttributeError):
        r = 0
    return r if _sane(r) else pkr_per_usd()


def launch_rate(launch, plan) -> float:
    """The rate a campaign was created with on Meta; else its plan's."""
    try:
        r = float((launch.objects or {}).get("pkr_per_usd") or 0) if launch is not None else 0
    except (TypeError, ValueError):
        r = 0
    return r if _sane(r) else plan_rate(plan)
