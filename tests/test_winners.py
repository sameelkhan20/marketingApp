"""Your winners: the store's best past ads are read from Meta and given to the planner."""

import json

from backend import meta_sync, planner
from backend.estimator import estimate_orders, estimate_to_dict
from backend.meta_client import MetaError


def _row(ad_id, name, spend, purchases, value=0.0):
    return {"ad_id": ad_id, "ad_name": name, "adset_name": "Broad", "spend": str(spend), "impressions": "10000",
            "inline_link_clicks": "150", "actions": [{"action_type": "purchase", "value": str(purchases)}],
            "action_values": [{"action_type": "purchase", "value": str(value)}]}


def test_top_ads_order_and_filter():
    rows = [_row("A", "few", 50, 2), _row("B", "cheap", 100, 10, 900), _row("C", "most", 300, 12), _row("D", "tie", 80, 10)]
    top = meta_sync.top_ads(rows, "USD")
    assert [t["ad_id"] for t in top] == ["C", "D", "B"]  # 2 purchases is too few; ties by cost per purchase
    assert top[2]["roas"] == 9.0 and top[1]["cpa_usd"] == 8.0
    pkr = meta_sync.top_ads([_row("P", "pkr", 28000, 4)], "PKR")
    assert pkr[0]["spend_usd"] == 100.0  # 28,000 PKR at the fixed test rate of 280


class FakeAds:
    def __init__(self, token):
        if not token:
            raise MetaError("Meta access token is not set")

    def account(self, act):
        return {"currency": "USD"}

    def ad_insights(self, act, since, until):
        return [_row("W1", "Gift box carousel", 200, 20, 1600), _row("W2", "Quality close-up", 150, 8)]

    def ad_copy(self, ad_id):
        if ad_id == "W2":
            raise MetaError("creative not readable")
        return {"title": "The set they will keep", "body": "24 pieces, mirror polish, gift box included", "carousel": True}


def test_sync_winners_per_store_and_into_the_prompt(client, auth, monkeypatch):
    monkeypatch.setattr(meta_sync, "MetaClient", FakeAds)
    bid = client.post("/api/brands", json={"values": {"name": "Winners Store", "meta_access_token": "EAAw",
                                                      "meta_ad_account_id": "act_9"}}, headers=auth).json()["id"]
    r = client.post(f"/api/brands/{bid}/winners", headers=auth)
    assert r.status_code == 200 and r.json()["winners"] == 2
    wins = client.get(f"/api/brands/{bid}/winners", headers=auth).json()
    assert wins[0]["title"] == "The set they will keep" and wins[0]["carousel"] is True
    assert "title" not in wins[1]  # its copy could not be read: kept without it
    assert client.get("/api/brands/0/winners", headers=auth).json() == []  # another store is untouched
    text = planner._user_prompt({"budget_pkr": 100000, "country": "US", "product": "Cutlery", "niche": "kitchen_cutlery",
                                 "price_usd": 40, "duration_days": 30}, estimate_to_dict(estimate_orders(100000, "US")), [], wins)
    assert "Gift box carousel" in text and "20 purchases" in text and "mirror polish" in text
    client.delete(f"/api/brands/{bid}", headers=auth)


def test_plan_gets_the_stores_winners(client, auth, monkeypatch):
    from backend import main
    seen = {}
    monkeypatch.setattr(meta_sync, "MetaClient", FakeAds)
    bid = client.post("/api/brands", json={"values": {"name": "Winners Two", "meta_access_token": "EAAw",
                                                      "meta_ad_account_id": "act_9"}}, headers=auth).json()["id"]
    client.post(f"/api/brands/{bid}/winners", headers=auth)

    def fake_generate(inputs, estimate, rules, winners=None):
        seen["winners"] = winners
        return planner._template_plan(inputs, estimate, rules)
    monkeypatch.setattr(main, "generate_plan", fake_generate)
    body = {"budget_pkr": 100000, "country": "US", "product": "Cutlery set", "niche": "kitchen_cutlery", "brand_id": bid,
            "offer": "Free gift box", "delivery_days": 8}
    p = client.post("/api/plans", json=body, headers=auth).json()
    assert [w["ad_id"] for w in seen["winners"]] == ["W1", "W2"]
    assert p["inputs"]["offer"] == "Free gift box" and p["inputs"]["delivery_days"] == 8
    assert client.get(f"/api/brief?brand_id={bid}", headers=auth).json()["offer"] == "Free gift box"
    client.delete(f"/api/plans/{p['id']}", headers=auth)
    client.delete(f"/api/brands/{bid}", headers=auth)
    assert json.loads("[]") == []
