"""Brands: each store has its own Meta connection, and everything about a plan uses its store's."""

import pytest

from backend import brands, monitor, settings_store
from backend.db import Launch, Plan, Setting, get_session
from tests.fakes import FakeMeta


@pytest.fixture
def cutlery(client, auth):
    r = client.post("/api/brands", json={"values": {
        "name": "Cutlery Nest", "meta_access_token": "EAAcutlerytoken123", "meta_ad_account_id": "act_222",
        "meta_page_id": "PAGE2", "meta_pixel_id": "PX2", "store_url": "https://cutlery.test",
        "creative_image_urls": "https://cutlery.test/a.jpg"}}, headers=auth)
    assert r.status_code == 200, r.text
    b = r.json()
    yield b["id"]
    with get_session() as s:
        for p in s.query(Plan).filter(Plan.brand_id == b["id"]).all():
            p.brand_id = 0
        s.commit()
    client.delete(f"/api/brands/{b['id']}", headers=auth)


def test_brand_is_saved_with_its_token_encrypted(client, auth, cutlery):
    b = client.get(f"/api/brands/{cutlery}", headers=auth).json()
    assert b["name"] == "Cutlery Nest" and b["meta_access_token"] == "********" and b["meta_ad_account_id"] == "act_222"
    with get_session() as s:
        raw = s.get(Setting, "brands").value
        assert "EAAcutlerytoken123" not in raw
        v = brands.settings_for(s, cutlery)
        assert v["meta_access_token"] == "EAAcutlerytoken123" and v["store_url"] == "https://cutlery.test"
        assert brands.settings_for(s, 0)["meta_ad_account_id"] != "act_222"  # the main store is untouched
    listed = {x["name"]: x for x in client.get("/api/brands", headers=auth).json()}
    assert listed["Cutlery Nest"]["connected"] and any(x["main"] for x in listed.values())
    # posting the mask back keeps the token
    client.post(f"/api/brands/{cutlery}", json={"values": {"meta_access_token": "********", "meta_page_id": "PAGE3"}}, headers=auth)
    with get_session() as s:
        v = brands.settings_for(s, cutlery)
        assert v["meta_access_token"] == "EAAcutlerytoken123" and v["meta_page_id"] == "PAGE3"


def test_plan_launches_with_its_brand(client, auth, cutlery, monkeypatch, active_meta):
    from backend import main
    body = {"budget_pkr": 100000, "country": "PK", "product": "Cutlery set", "niche": "kitchen_cutlery",
            "price_usd": 30, "brand_id": cutlery}
    p = client.post("/api/plans", json=body, headers=auth).json()
    assert p["inputs"]["brand_id"] == cutlery
    with get_session() as s:
        s.get(Plan, p["id"]).status = "approved"; s.commit()
    seen = {}
    monkeypatch.setattr(main.readiness, "check_plan", lambda s, plan, live=False: {"ready": True, "checks": []})

    def fake_launch(inputs, content, settings, plan_id, images, save_audiences=None):
        seen.update(settings)
        save_audiences({"pixel": "PX2", "visitors": "AUDV", "buyers": "AUDB"})
        return {"campaign_id": "C-CUT", "adsets": [], "ads": [], "notes": [], "currency": "PKR", "account": "act_222"}
    monkeypatch.setattr(main, "launch_plan", fake_launch)
    assert client.post(f"/api/plans/{p['id']}/launch", headers=auth).status_code == 200
    assert seen["meta_access_token"] == "EAAcutlerytoken123" and seen["meta_page_id"] == "PAGE2"
    with get_session() as s:
        l = s.query(Launch).filter(Launch.campaign_id == "C-CUT").one()
        assert l.objects["brand_id"] == cutlery and brands.launch_brand(s, l) == cutlery
        assert brands.settings_for(s, cutlery)["aud_visitors"] == "AUDV"          # saved on the brand
        assert settings_store.get_all(s)["aud_visitors"] != "AUDV"               # not on the main store
        l.status = "completed"; s.commit()


def test_monitor_uses_each_brands_token(cutlery, plan, monkeypatch):
    with get_session() as s:
        for l in s.query(Launch).all():
            l.status = "completed"
        settings_store.set_many(s, {"meta_access_token": "EAAmaintoken", "meta_ad_account_id": "act_111"})
        s.get(Plan, plan).brand_id = cutlery
        s.add(Launch(plan_id=plan, campaign_id="FAKE_B", objects={"currency": "USD", "brand_id": cutlery,
                                                                 "adsets": [], "ads": []}, status="active"))
        s.commit()
    tokens = []

    class Spy(FakeMeta):
        def __init__(self, token="x"):
            super().__init__(token)
            tokens.append(token)
    monkeypatch.setattr(monitor, "MetaClient", Spy)
    r = monitor.run_once()
    assert "EAAcutlerytoken123" in tokens and "EAAmaintoken" not in tokens, (tokens, r)
    assert any(c["campaign_id"] == "FAKE_B" for c in r["checked"])
    with get_session() as s:
        for l in s.query(Launch).filter(Launch.campaign_id == "FAKE_B").all():
            l.status = "completed"
        settings_store.set_many(s, {"meta_access_token": "", "meta_ad_account_id": ""})


def test_brand_in_use_cannot_be_deleted(client, auth, cutlery):
    with get_session() as s:
        p = s.query(Plan).first()
        old = p.brand_id
        p.brand_id = cutlery; s.commit()
    assert client.delete(f"/api/brands/{cutlery}", headers=auth).status_code == 400
    assert client.delete("/api/brands/0", headers=auth).status_code == 400
    with get_session() as s:
        s.query(Plan).first().brand_id = old; s.commit()


def test_unknown_brand_refused_and_owner_only(client, auth):
    body = {"budget_pkr": 100000, "country": "PK", "product": "Cutlery set", "niche": "kitchen_cutlery", "brand_id": 999}
    assert client.post("/api/plans", json=body, headers=auth).status_code == 400
    client.post("/api/users/register", json={"email": "brandstaff@example.com", "password": "StaffPass9"}, headers=auth)
    tok = client.post("/api/auth/login", json={"email": "brandstaff@example.com", "password": "StaffPass9"}).json()
    h = {"Authorization": f"Bearer {tok['access_token']}"}
    assert client.post("/api/brands", json={"values": {"name": "X"}}, headers=h).status_code == 403
    assert client.get("/api/brands", headers=h).status_code == 200  # staff can pick a brand for a plan
