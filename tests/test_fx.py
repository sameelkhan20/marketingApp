"""Live USD->PKR rate: fetched, cached, saved, and each plan/launch keeps its own rate."""

import time

import pytest

from backend import fx


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._b = status, body

    def json(self):
        return self._b


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setattr(fx, "FX_LIVE", True)
    from backend.db import Setting, get_session
    with get_session() as s:
        s.query(Setting).filter(Setting.key == "fx_pkr_per_usd").delete(); s.commit()
    fx._cache.clear()
    fx._last_try[0] = 0
    calls = []

    def install(resp):
        def get(url, timeout=None):
            calls.append(url)
            if isinstance(resp, Exception):
                raise resp
            return resp
        monkeypatch.setattr(fx.requests, "get", get)
        return calls
    yield install
    fx._cache.clear()


def test_live_rate_is_fetched_and_cached(live):
    calls = live(_Resp(200, {"result": "success", "rates": {"PKR": 281.37}}))
    assert fx.pkr_per_usd() == 281.37
    assert fx.info()["source"] == "live"
    fx.pkr_per_usd()
    assert len(calls) == 1  # cached for 6 hours


def test_feed_failure_falls_back_to_saved_then_env(live):
    live(_Resp(200, {"result": "success", "rates": {"PKR": 279.5}}))
    assert fx.pkr_per_usd() == 279.5
    fx._cache["fetched_at"] = time.time() - 7 * 3600  # stale
    fx._last_try[0] = 0
    import requests
    live(requests.ConnectionError("offline"))
    i = fx.info()
    assert i["rate"] == 279.5 and i["source"] == "saved"
    # nonsense from the feed is never used
    fx._cache.clear(); fx._last_try[0] = 0
    from backend.db import Setting, get_session
    with get_session() as s:
        s.query(Setting).filter(Setting.key == "fx_pkr_per_usd").delete(); s.commit()
    live(_Resp(200, {"result": "success", "rates": {"PKR": 0.5}}))
    assert fx.info() == {"rate": 280.0, "updated": "", "source": "fixed"}


def test_plan_and_launch_keep_their_rate():
    class P:
        estimate = {"assumptions": {"pkr_per_usd": 300}}

    class L:
        objects = {"pkr_per_usd": 290}
    assert fx.plan_rate(P()) == 300 and fx.launch_rate(L(), P()) == 290
    class Old:
        estimate = {}
    assert fx.plan_rate(Old()) == 280.0  # tests run with the fixed rate


def test_estimate_records_the_rate(client, auth):
    e = client.post("/api/estimate", json={"budget_pkr": 280000, "country": "US", "product": "Jacket"}, headers=auth).json()
    assert e["assumptions"]["pkr_per_usd"] == 280 and e["budget_usd"] == 1000
    m = client.get("/api/meta", headers=auth).json()
    assert m["pkr_per_usd"] == 280 and m["fx"]["source"] == "fixed"
