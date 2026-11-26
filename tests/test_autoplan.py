"""Auto plan: budget and days in; the app picks catalogue or product, country, price and brief, and says why.
Catalogue campaigns are built from a product set; Start switches a paused campaign on."""

import pytest

import backend.launcher as L
from backend import autoplan, brief_suggest, catalog, settings_store
from backend.db import AccountStat, Launch, Plan, StoreProduct, get_session
from backend.meta_client import MetaError
from tests.test_launcher import CONTENT, PLAN_INPUTS, SETTINGS, Recorder


# ---------- reading the catalogue ----------

class FakeCatalog:
    readable = True

    def __init__(self, token="x"):
        pass

    def account(self, act):
        return {"currency": "USD"}

    def adsets_promoting(self, act):
        return [{"id": "A1", "promoted_object": {"product_set_id": "PS1"}, "campaign": {"name": "Catalogue Sept"}},
                {"id": "A2", "promoted_object": {"product_set_id": "PS2"}, "campaign": {"name": "Dynamic Best Seller"}},
                {"id": "A3", "promoted_object": {"pixel_id": "PX"}, "campaign": {"name": "Plain"}},
                # made in Ads Manager: the product set sits on the ad, not on the ad set
                {"id": "A4", "promoted_object": {"pixel_id": "PX"}, "campaign": {"name": "Sales Catalogue NEW Oct"}},
                {"id": "A5", "promoted_object": {"pixel_id": "PX"}, "campaign": {"name": "Old August"}}]

    def ads_product_sets(self, act):
        return [{"adset_id": "A4", "creative": {"product_set_id": "PS1"}},
                {"adset_id": "A5", "creative": {"product_set_id": "GONE"}},     # a deleted product set
                {"adset_id": "A3", "creative": {}}]

    def adset_insights(self, act, since, until):
        return [{"adset_id": "A1", "spend": "100", "inline_link_clicks": "50", "actions": []},
                {"adset_id": "A2", "spend": "80", "inline_link_clicks": "40",
                 "actions": [{"action_type": "purchase", "value": "3"}],
                 "action_values": [{"action_type": "purchase", "value": "240"}]},
                {"adset_id": "A4", "spend": "50", "inline_link_clicks": "20",
                 "actions": [{"action_type": "purchase", "value": "1"}]},
                {"adset_id": "A5", "spend": "500", "inline_link_clicks": "300",
                 "actions": [{"action_type": "purchase", "value": "9"}]}]

    def product_set(self, ps):
        if not FakeCatalog.readable or ps == "GONE":
            raise MetaError("(#100) This application has not been approved to use this api")
        return {"name": {"PS1": "All products", "PS2": "Best sellers"}[ps], "product_count": 29,
                "product_catalog": {"id": "CAT9"}}


def test_product_sets_ranked_by_what_they_sold(monkeypatch):
    import backend.meta_client as MC
    monkeypatch.setattr(MC, "MetaClient", FakeCatalog)
    FakeCatalog.readable = True
    got = catalog.product_sets({"meta_access_token": "t", "meta_ad_account_id": "1"})
    by_id = {s["id"]: s for s in got["sets"]}
    assert got["readable"] and by_id["GONE"].get("gone") and by_id["GONE"]["product_count"] == 0
    # PS1 now also counts the campaign whose product set sits on its ads
    assert by_id["PS1"]["purchases"] == 1 and by_id["PS1"]["spend_usd"] == 150.0
    assert "Sales Catalogue NEW Oct" in by_id["PS1"]["campaigns"]
    best = by_id["PS2"]
    assert best["name"] == "Best sellers" and best["purchases"] == 3 and best["catalog_id"] == "CAT9"
    FakeCatalog.readable = False
    got = catalog.product_sets({"meta_access_token": "t", "meta_ad_account_id": "1"})
    assert not got["readable"] and "catalog_management" in got["note"] and got["sets"][0]["catalog_id"] == ""


# ---------- deciding ----------

@pytest.fixture
def connected(monkeypatch):
    import backend.meta_client as MC
    monkeypatch.setattr(MC, "MetaClient", FakeCatalog)
    FakeCatalog.readable = True
    monkeypatch.setattr(brief_suggest, "suggest", lambda s, b, url="": {
        "offer": "Free custom branding", "usp": "Full-grain leather", "delivery_days": 7, "returns": "",
        "social_proof": "", "what_your_ads_do": "Your ads lead with free branding."})
    with get_session() as s:
        old = settings_store.get_all(s)
        settings_store.set_many(s, {"meta_access_token": "EAAx", "meta_ad_account_id": "act_1",
                                    "store_url": "https://myapron.co.uk/", "meta_page_id": "PAGE", "meta_pixel_id": "PX"})
        s.query(AccountStat).delete()
        # the UK does far better than the US for this store
        s.add(AccountStat(country="GB", since="a", until="b", spend_usd=500, impressions=60000, clicks=2400, purchases=40,
                          revenue_usd=3200, cpm_usd=8, ctr=0.04, cvr=0.02, confidence="medium"))
        s.add(AccountStat(country="US", since="a", until="b", spend_usd=900, impressions=40000, clicks=600, purchases=3,
                          revenue_usd=240, cpm_usd=30, ctr=0.015, cvr=0.005, confidence="low"))
        s.commit()
    yield
    with get_session() as s:
        s.query(AccountStat).delete()
        s.query(StoreProduct).delete()
        s.commit()
        settings_store.set_many(s, {k: old[k] for k in ("meta_access_token", "meta_ad_account_id", "store_url",
                                                         "meta_page_id", "meta_pixel_id")})


def test_decides_catalogue_country_price_and_says_why(connected):
    with get_session() as s:
        d = autoplan.decide(s, 0, 300000, 14)
    i, x = d["inputs"], d["extra"]
    assert d["scope"] == "catalogue" and x["product_set_id"] == "PS2" and x["catalog_id"] == "CAT9"
    assert i["country"] == "GB"                                   # its own results say the UK sells best
    assert i["price_usd"] == 80.0                                 # 3,440 of sales / 43 orders
    assert i["offer"] == "Free custom branding" and i["delivery_days"] == 7
    topics = [t["topic"] for t in x["decisions"]]
    assert topics[:2] == ["What to advertise", "Category"] and "Where" in topics and "Selling points" in topics
    assert "3 sales" in x["decisions"][0]["why"]


def test_a_proven_product_beats_a_catalogue_that_never_sold(connected, monkeypatch):
    monkeypatch.setattr(FakeCatalog, "adset_insights", lambda self, *a: [])
    with get_session() as s:
        s.add(StoreProduct(brand_id=0, key="x:1", title="Leather BBQ Apron", url="https://myapron.co.uk/p/bbq",
                           in_stock=1, price_usd=84.99, cost_usd=30, shipping_usd=10, ids=[], image="i",
                           meta={"spend_usd": 20, "purchases": 4, "clicks": 60}))
        s.commit()
        d = autoplan.decide(s, 0, 300000, 14)
    assert d["scope"] == "product" and d["inputs"]["product"] == "Leather BBQ Apron"
    assert d["inputs"]["landing_url"] == "https://myapron.co.uk/p/bbq" and d["inputs"]["price_usd"] == 84.99


def test_nothing_to_go_on_is_said_plainly(connected, monkeypatch):
    monkeypatch.setattr(FakeCatalog, "adsets_promoting", lambda self, act: [])
    with get_session() as s, pytest.raises(autoplan.AutoPlanError, match="Not enough"):
        autoplan.decide(s, 0, 300000, 14)


def test_auto_plan_endpoint_makes_a_catalogue_plan(client, auth, connected):
    r = client.post("/api/plans/auto", json={"brand_id": 0, "budget_pkr": 300000, "duration_days": 14}, headers=auth)
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["auto"]["scope"] == "catalogue" and p["auto"]["product_set_name"] == "Best sellers"
    assert p["inputs"]["country"] == "GB"
    assert all(c["format"] == "Catalogue" for c in p["versions"][-1]["content"]["creatives"])
    with get_session() as s:
        from backend import readiness
        plan = s.get(Plan, p["id"])
        plan.status = "approved"; s.commit()
        checks = {c["key"]: c for c in readiness.check_plan(s, plan, live=False)["checks"]}
    assert checks["catalogue"]["ok"] and "images" not in checks


# ---------- building a catalogue campaign ----------

@pytest.fixture
def recorder(monkeypatch):
    """The launcher's recording fake: no call ever reaches Meta."""
    Recorder.currency, Recorder.fail_on_adset, Recorder.audience_fail = "USD", None, False
    Recorder.last = None
    monkeypatch.setattr(L, "MetaClient", Recorder)
    return Recorder


def test_launcher_builds_catalogue_ads(recorder):
    inputs = {**PLAN_INPUTS, "scope": "catalogue", "product_set_id": "PS2", "catalog_id": "CAT9"}
    content = {**CONTENT, "creatives": [{**c, "format": "Catalogue", "headline": "{{product.name}}"} for c in CONTENT["creatives"]]}
    r = L.launch_plan(inputs, content, {**SETTINGS, "creative_image_urls": ""}, 7)
    rec = Recorder.last
    assert rec.campaigns[0]["catalog_id"] == "CAT9"
    assert all(a["promoted_object"] == {"product_set_id": "PS2", "pixel_id": "PX", "custom_event_type": "PURCHASE"}
               for a in rec.adsets)
    cr = rec.creatives[0]
    assert cr["product_set_id"] == "PS2" and cr["object_story_spec"]["template_data"]["name"] == "{{product.name}}"
    assert "image_hash" not in str(cr) and len(r["ads"]) == len(rec.adsets) * 2     # no pictures needed


def test_catalogue_plan_without_catalogue_access_stops_before_meta(recorder):
    with pytest.raises(MetaError, match="catalog_management"):
        L.launch_plan({**PLAN_INPUTS, "scope": "catalogue", "product_set_id": "PS2", "catalog_id": ""}, CONTENT, SETTINGS, 7)
    assert recorder.last.campaigns == []          # stopped before anything was created on Meta


# ---------- Start ----------

def test_start_switches_on_ads_then_ad_sets_then_campaign(client, auth, plan, monkeypatch):
    from backend import main
    calls = []

    class Switch:
        def __init__(self, token="x"):
            pass

        def account(self, act):
            return {"currency": "USD", "account_status": 1}

        def update_object(self, oid, **f):
            calls.append((oid, f["status"]))
            return {"success": True}
    monkeypatch.setattr(main, "MetaClient", Switch)
    with get_session() as s:
        settings_store.set_many(s, {"meta_access_token": "EAAx"})
        l = Launch(plan_id=plan, campaign_id="CAMP9", status="active",
                   objects={"account": "act_1", "adsets": [{"id": "AS1"}], "ads": [{"id": "AD1"}, {"id": "AD2"}]})
        s.add(l); s.commit(); lid = l.id
    r = client.post(f"/api/launches/{lid}/start", headers=auth)
    assert r.status_code == 200, r.text
    assert calls == [("AD1", "ACTIVE"), ("AD2", "ACTIVE"), ("AS1", "ACTIVE"), ("CAMP9", "ACTIVE")]
    assert client.post(f"/api/launches/{lid}/start", headers=auth).status_code == 400      # only once
    client.post("/api/users/register", json={"email": "startstaff@example.com", "password": "StaffPass9"}, headers=auth)
    tok = client.post("/api/auth/login", json={"email": "startstaff@example.com", "password": "StaffPass9"}).json()
    assert client.post(f"/api/launches/{lid}/start", headers={"Authorization": f"Bearer {tok['access_token']}"}).status_code == 403
    with get_session() as s:
        s.get(Launch, lid).status = "completed"; s.commit()
        settings_store.set_many(s, {"meta_access_token": ""})


def test_own_results_beat_an_unproven_benchmark(connected):
    """The US has the store's own (modest) results; the UK has none: the UK benchmark is cut, so it must
    be clearly better to win."""
    with get_session() as s:
        s.query(AccountStat).filter(AccountStat.country == "GB").delete(); s.commit()
        code, why = autoplan._country(s, 0, 300000, 14, "other", 80.0)
    head = why.split(";")[0]          # the chosen market's own line
    assert code == "GB" and "cut by 30%" in head and "United States ~4 (your own results)" in why
    # with decent US results of its own, the US wins over the cut UK benchmark
    with get_session() as s:
        us = s.get(AccountStat, "US")
        us.cvr, us.purchases, us.revenue_usd, us.cpm_usd, us.ctr = 0.03, 40, 3200, 12, 0.03
        s.commit()
        code, why = autoplan._country(s, 0, 300000, 14, "other", 80.0)
    assert code == "US" and "your own results" in why.split(";")[0]


def test_an_empty_product_set_is_never_chosen(connected, monkeypatch):
    real = FakeCatalog.product_set

    def sizes(self, ps):
        d = real(self, ps)
        return {**d, "product_count": 0 if ps == "PS2" else 29}     # the best seller set is now empty
    monkeypatch.setattr(FakeCatalog, "product_set", sizes)
    with get_session() as s:
        d = autoplan.decide(s, 0, 300000, 14)
    assert d["extra"]["product_set_id"] == "PS1"


def _stock(s, key, title, units, ad_sales=0, rank=0):
    s.add(StoreProduct(brand_id=0, key=key, title=title, url=f"https://myapron.co.uk/p/{key}", in_stock=1,
                       price_usd=70, cost_usd=0, shipping_usd=0, ids=[], image="i",
                       meta={"store_units": units, "store_rank": rank, "store_window": "last 365 days",
                             "purchases": ad_sales, "spend_usd": 10 if ad_sales else 0}))


def test_a_clear_best_seller_beats_the_catalogue(connected):
    with get_session() as s:
        _stock(s, "x:hero", "Brown Canvas Baker Apron", 15, rank=1)
        _stock(s, "x:two", "BBQ Work Apron", 6, ad_sales=1, rank=2)
        _stock(s, "x:three", "Forge Apron", 2, rank=3)
        s.commit()
        d = autoplan.decide(s, 0, 300000, 14)
    assert d["scope"] == "product" and d["inputs"]["product"] == "Brown Canvas Baker Apron"
    assert "15 of your 23 sales" in d["extra"]["decisions"][0]["why"]
    assert {x["topic"]: x for x in d["extra"]["decisions"]}["Decided by"]["choice"] == "The app's rules"


def test_sales_spread_thin_go_to_the_catalogue(connected):
    with get_session() as s:
        for n in range(6):
            _stock(s, f"x:{n}", f"Apron {n}", 2)
        s.commit()
        d = autoplan.decide(s, 0, 300000, 14)
    assert d["scope"] == "catalogue" and "spread over many products" in d["extra"]["decisions"][0]["why"]


def test_ai_manager_decides_but_only_among_real_options(connected, monkeypatch):
    from backend import llm
    with get_session() as s:
        _stock(s, "x:hero", "Brown Canvas Baker Apron", 15, rank=1)
        s.commit()
    seen = {}
    monkeypatch.setattr(llm, "configured", lambda: True)

    def manager(system, prompt, schema, feature, **kw):
        seen["prompt"] = prompt
        return {"advertise": "catalogue:PS1", "country": "GB", "why_what": "Christmas gifts sell across the range.",
                "why_country": "Your UK results are strongest.", "strategy": "Test 4 days, then scale the winner."}
    monkeypatch.setattr(llm, "structured", manager)
    with get_session() as s:
        d = autoplan.decide(s, 0, 300000, 14)
    assert d["scope"] == "catalogue" and d["extra"]["product_set_id"] == "PS1" and d["inputs"]["country"] == "GB"
    topics = {x["topic"]: x for x in d["extra"]["decisions"]}
    assert topics["How to run it"]["why"] == "Test 4 days, then scale the winner."
    assert topics["Decided by"]["choice"] == "AI marketing manager"
    assert '"share_of_store_sales_pct": 100.0' in seen["prompt"] and '"markets"' in seen["prompt"]

    # an option that does not exist: the app's own rules decide instead
    monkeypatch.setattr(llm, "structured", lambda *a, **k: {"advertise": "product:nope", "country": "GB",
                                                             "why_what": "x", "why_country": "y", "strategy": ""})
    with get_session() as s:
        d = autoplan.decide(s, 0, 300000, 14)
    assert {x["topic"]: x for x in d["extra"]["decisions"]}["Decided by"]["choice"] == "The app's rules"


def test_plan_keeps_what_it_was_based_on_and_the_managers_ideas(client, auth, connected, monkeypatch):
    from backend import llm
    with get_session() as s:
        _stock(s, "x:hero", "Brown Canvas Baker Apron", 15, rank=1)
        _stock(s, "x:two", "BBQ Work Apron", 5, rank=2)
        s.commit()
    monkeypatch.setattr(llm, "configured", lambda: True)
    monkeypatch.setattr(llm, "structured", lambda system, *a, **k: (
        {"advertise": "product:x:hero", "country": "US", "why_what": "It is 75% of your sales.",
         "why_country": "Your US results.", "strategy": "Test, then scale.",
         "other_ideas": ["Push the Chef Apron collection before Christmas.", "  "]}
        if "marketing manager" in system else {}))
    r = client.post("/api/plans/auto", json={"brand_id": 0, "budget_pkr": 300000, "duration_days": 14}, headers=auth)
    assert r.status_code == 200, r.text
    auto = r.json()["auto"]
    assert auto["ideas"] == ["Push the Chef Apron collection before Christmas."]
    based = " ".join(auto["based_on"])
    assert "Brown Canvas Baker Apron 15 (75%)" in based and "Budget: PKR 300,000 over 14 days" in based
    assert "Markets:" in based


def test_cost_limit_shapes_the_plan(client, auth, connected):
    r = client.post("/api/plans/auto", json={"brand_id": 0, "budget_pkr": 300000, "duration_days": 14,
                                              "max_cost_per_order_pkr": 3000}, headers=auth)
    assert r.status_code == 200, r.text
    p = r.json()
    rate = p["estimate"]["assumptions"]["pkr_per_usd"]
    content = p["versions"][-1]["content"]
    assert p["auto"]["max_cpa_pkr"] == 3000
    assert content["monitoring_targets"]["max_cpa_usd"] == round(3000 / rate, 2)
    assert "PKR 3,000" in content["kill_rules"][0]
    why = {x["topic"]: x for x in p["auto"]["decisions"]}["Your cost limit"]["why"]
    assert "needs about 100 orders" in why
    assert any("PKR 3,000" in line for line in p["auto"]["based_on"])


def test_cost_limit_becomes_a_meta_cost_cap(recorder):
    L.launch_plan({**PLAN_INPUTS, "max_cpa_pkr": 2800}, CONTENT, SETTINGS, 7)
    sets = recorder.last.adsets
    assert all(a["bid_strategy"] == "COST_CAP" and a["bid_amount"] == 1000 for a in sets)   # $10 at the test rate of 280
    L.launch_plan(PLAN_INPUTS, CONTENT, SETTINGS, 8)
    assert all(a["bid_strategy"] == "LOWEST_COST_WITHOUT_CAP" and "bid_amount" not in a for a in recorder.last.adsets)
    r = L.launch_plan({**PLAN_INPUTS, "max_cpa_pkr": 2800}, CONTENT, {**SETTINGS, "meta_pixel_id": ""}, 9)
    assert all("bid_amount" not in a for a in recorder.last.adsets) and any("without a pixel" in n for n in r["notes"])


def test_owner_limit_alerts_with_no_slack():
    from backend.rules import evaluate
    row = {"level": "adset", "object_id": "A", "name": "Broad", "effective_status": "ACTIVE", "spend_usd": 33.0,
           "purchases": 3, "cpa_usd": 11.0, "impressions": 100, "clicks": 5, "ctr": 0.05, "frequency": 1.0, "roas": 3.0}
    kinds = lambda t: [f["kind"] for f in evaluate([row], t, 10, 5, "ACTIVE", [])]
    assert "high_cpa" not in kinds({"max_cpa_usd": 10})
    assert "high_cpa" in kinds({"max_cpa_usd": 10, "strict_cpa": True})
