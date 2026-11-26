"""Each store's plans use that store's own two years of history; syncing one store never touches another."""

from backend import meta_sync
from backend.db import AccountStat, StoreStat, get_session
from backend.history import account_overrides
from tests.test_winners import FakeAds


class FakeHistory(FakeAds):
    def insights_by_country(self, act, since, until):
        return [{"country": "US", "date_start": "2026-01-01", "spend": "500", "impressions": "50000",
                 "inline_link_clicks": "1000", "actions": [{"action_type": "purchase", "value": "25"}],
                 "action_values": [{"action_type": "purchase", "value": "2500"}]}]


def test_store_sync_uses_its_own_rows(client, auth, monkeypatch):
    monkeypatch.setattr(meta_sync, "MetaClient", FakeHistory)
    with get_session() as s:
        s.query(AccountStat).delete()
        s.add(AccountStat(country="US", since="a", until="b", spend_usd=1, impressions=20000, clicks=10, purchases=0,
                          revenue_usd=0, cpm_usd=9, ctr=0.01, cvr=0, confidence="low"))
        s.commit()
    bid = client.post("/api/brands", json={"values": {"name": "History Store", "meta_access_token": "EAAh",
                                                      "meta_ad_account_id": "act_7"}}, headers=auth).json()["id"]
    assert account_overrides("US", "other", 60, bid) is None          # not synced yet: benchmarks
    r = client.post(f"/api/brands/{bid}/sync", headers=auth)
    assert r.status_code == 200 and r.json()["countries"] == 1 and r.json()["winners"] == 2
    ov = account_overrides("US", "other", 60, bid)
    assert ov and "25 purchases" in ov["source"]
    rows = client.get(f"/api/meta/stats?brand_id={bid}", headers=auth).json()
    assert rows[0]["purchases"] == 25
    main = client.get("/api/meta/stats", headers=auth).json()
    assert main[0]["impressions"] == 20000                            # the main store's history is untouched
    client.delete(f"/api/brands/{bid}", headers=auth)
    with get_session() as s:
        assert s.query(StoreStat).filter(StoreStat.brand_id == bid).count() == 0
        s.query(AccountStat).delete(); s.commit()
