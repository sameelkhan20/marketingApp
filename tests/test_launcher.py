"""Launcher: plan -> Meta objects. Uses a recording fake client (no network)."""

import time

import pytest

import backend.launcher as L
from backend.meta_client import MetaError, budget_minor_units

PLAN_INPUTS = {"budget_pkr": 280000, "country": "us", "product": "Biker jacket", "niche": "leather_jackets",
               "price_usd": 120, "duration_days": 10}
CONTENT = {
    "ad_sets": [
        {"name": "Broad", "audience": "US, 22-50, Advantage+", "placements": "auto", "daily_budget_pct": 60, "optimization": "Purchase"},
        {"name": "Interest", "audience": "US, 25-45, interests: leather", "placements": "feeds", "daily_budget_pct": 40, "optimization": "Purchase"},
    ],
    "creatives": [
        {"format": "1:1", "hook": "h", "primary_text": "t1", "headline": "H1", "cta": "Shop Now", "visual_prompt": "v"},
        {"format": "9:16", "hook": "h", "primary_text": "t2", "headline": "H2", "cta": "Learn More", "visual_prompt": "v"},
    ],
}
SETTINGS = {"meta_access_token": "tok", "meta_ad_account_id": "123", "meta_pixel_id": "PX", "meta_page_id": "PAGE",
            "store_url": "https://shop.example.com", "creative_image_urls": "https://img/1.jpg"}


class Recorder:
    currency = "USD"
    fail_on_adset = None

    def __init__(self, token):
        self.campaigns, self.adsets, self.creatives, self.ads, self.updates = [], [], [], [], []
        self.audiences = []
        Recorder.last = self

    def account(self, act):
        return {"currency": Recorder.currency}

    audience_fail = False

    def create_custom_audience(self, act, name, rule, description=""):
        if Recorder.audience_fail:
            raise MetaError("Meta API error (2650): accept the Custom Audience terms")
        self.audiences.append({"name": name, "rule": rule})
        return f"AUD{len(self.audiences)}"

    def create_campaign(self, act, name, **kw):
        self.campaigns.append({"name": name, **kw})
        return "C1"

    def create_adset(self, act, **f):
        if Recorder.fail_on_adset is not None and len(self.adsets) == Recorder.fail_on_adset:
            raise MetaError("Meta API error (100): Invalid parameter")
        self.adsets.append(f)
        return f"AS{len(self.adsets)}"

    def create_creative(self, act, **f):
        self.creatives.append(f)
        return f"CR{len(self.creatives)}"

    def create_ad(self, act, name, adset_id, creative_id):
        self.ads.append((name, adset_id, creative_id))
        return f"AD{len(self.ads)}"

    def update_object(self, oid, **f):
        self.updates.append((oid, f))
        return {}


@pytest.fixture(autouse=True)
def _fake(monkeypatch):
    Recorder.currency, Recorder.fail_on_adset, Recorder.audience_fail = "USD", None, False
    monkeypatch.setattr(L, "MetaClient", Recorder)


def test_usd_budget_split_and_targeting():
    r = L.launch_plan(PLAN_INPUTS, CONTENT, SETTINGS, 7)
    rec = Recorder.last
    # 280000 PKR / 280 / 10 days = $100/day -> 60% and 40% in cents
    assert [a["daily_budget"] for a in rec.adsets] == [6000, 4000]
    assert rec.adsets[0]["targeting"]["geo_locations"] == {"countries": ["US"]}
    assert (rec.adsets[0]["targeting"]["age_min"], rec.adsets[0]["targeting"]["age_max"]) == (22, 65)  # Advantage+ audience: Meta only accepts age_max 65 (age is a suggestion)
    assert rec.adsets[0]["targeting"].get("targeting_automation") == {"advantage_audience": 1}
    assert rec.adsets[1]["targeting"]["targeting_automation"] == {"advantage_audience": 0}
    assert rec.adsets[0]["optimization_goal"] == "OFFSITE_CONVERSIONS"
    assert rec.adsets[0]["promoted_object"] == {"pixel_id": "PX", "custom_event_type": "PURCHASE"}
    assert len(r["ads"]) == 4 and r["ads"][0]["creative_index"] == 0
    cta = rec.creatives[1]["object_story_spec"]["link_data"]["call_to_action"]["type"]
    assert cta == "LEARN_MORE"


def test_pkr_account_budget():
    Recorder.currency = "PKR"
    L.launch_plan(PLAN_INPUTS, CONTENT, SETTINGS, 7)
    # 28000 PKR/day -> 60% = 16800 PKR -> minor units x100
    assert Recorder.last.adsets[0]["daily_budget"] == 1680000


def test_unsupported_currency_raises_before_creating_anything():
    Recorder.currency = "EUR"
    with pytest.raises(MetaError):
        L.launch_plan(PLAN_INPUTS, CONTENT, SETTINGS, 7)
    assert Recorder.last.campaigns == []


def test_no_pixel_falls_back_to_link_clicks():
    r = L.launch_plan(PLAN_INPUTS, CONTENT, {**SETTINGS, "meta_pixel_id": ""}, 7)
    assert Recorder.last.adsets[0]["optimization_goal"] == "LINK_CLICKS"
    assert any("LINK_CLICKS" in n for n in r["notes"])


def test_missing_page_skips_ads():
    r = L.launch_plan(PLAN_INPUTS, CONTENT, {**SETTINGS, "meta_page_id": ""}, 7)
    assert r["ads"] == [] and len(r["adsets"]) == 2


def test_image_hash_preferred_over_url():
    L.launch_plan(PLAN_INPUTS, CONTENT, SETTINGS, 7, creative_images=[{"creative_index": 1, "image_hash": "H"}])
    lds = [c["object_story_spec"]["link_data"] for c in Recorder.last.creatives]
    assert lds[0].get("picture") == "https://img/1.jpg" and lds[1].get("image_hash") == "H"


def test_age_range_parsing():
    assert L._age_range("US 18-65+") == (18, 65)
    assert L._age_range("no ages here") == (18, 65)
    assert L._age_range("ages 70-80") == (18, 65)  # out of Meta's range -> default
    assert L._age_range("25–34 women") == (25, 34)


def test_budget_minor_units():
    assert budget_minor_units(12.345, "USD") == 1234 or budget_minor_units(12.345, "USD") == 1235
    assert budget_minor_units(500, "JPY") == 500


# ---------- behaviour that must hold for real launches ----------

def test_adsets_stop_at_plan_end():
    """Ad sets must carry an end_time, otherwise a daily budget spends forever past the plan."""
    before = int(time.time())
    L.launch_plan(PLAN_INPUTS, CONTENT, SETTINGS, 7)
    for a in Recorder.last.adsets:
        assert "end_time" in a, "ad set has no end_time: it would keep spending after the plan ends"
        end = int(a["end_time"])
        assert before + 10 * 86400 - 60 <= end <= before + 10 * 86400 + 3600


def test_budget_shares_normalised_to_100():
    """Claude may return shares that do not add up to 100; the total daily budget must still match the plan."""
    content = {**CONTENT, "ad_sets": [{**CONTENT["ad_sets"][0], "daily_budget_pct": 50},
                                       {**CONTENT["ad_sets"][1], "daily_budget_pct": 30}]}
    L.launch_plan(PLAN_INPUTS, content, SETTINGS, 7)
    total = sum(a["daily_budget"] for a in Recorder.last.adsets)
    assert abs(total - 10000) <= 2, f"daily total {total} cents, plan says 10000"


def test_campaign_declares_budget_sharing_flag():
    """Meta requires is_adset_budget_sharing_enabled when budgets live on ad sets (no CBO)."""
    L.launch_plan(PLAN_INPUTS, CONTENT, SETTINGS, 7)
    assert Recorder.last.campaigns[0].get("is_adset_budget_sharing_enabled") is False


def test_partial_failure_cleans_up_campaign():
    """If an ad set fails after the campaign exists, the orphan campaign must be deleted, not left behind."""
    Recorder.fail_on_adset = 1
    with pytest.raises(MetaError):
        L.launch_plan(PLAN_INPUTS, CONTENT, SETTINGS, 7)
    assert ("C1", {"status": "DELETED"}) in Recorder.last.updates


# ---------- retargeting ----------

RT = {**CONTENT, "ad_sets": CONTENT["ad_sets"] + [
    {"name": "Retargeting", "audience": "Store visitors and add-to-cart in the last 30 days, excluding buyers",
     "placements": "auto", "daily_budget_pct": 15, "optimization": "Purchase"}]}


def test_buyers_excluded_and_retargeting_uses_visitors():
    r = L.launch_plan(PLAN_INPUTS, RT, SETTINGS, 7)
    rec = Recorder.last
    names = [a["name"] for a in rec.audiences]
    assert names == ["Ads Planner: store visitors 30d", "Ads Planner: buyers 180d"]
    buyers_rule = rec.audiences[1]["rule"]["inclusions"]["rules"][0]
    assert buyers_rule["event_sources"] == [{"id": "PX", "type": "pixel"}] and buyers_rule["retention_seconds"] == 180 * 86400
    assert buyers_rule["filter"]["filters"][0] == {"field": "event", "operator": "eq", "value": "Purchase"}
    for a in rec.adsets:
        assert a["targeting"]["excluded_custom_audiences"] == [{"id": "AUD2"}]
    rt = rec.adsets[2]["targeting"]
    assert rt["custom_audiences"] == [{"id": "AUD1"}] and rt["targeting_automation"] == {"advantage_audience": 0}
    assert "custom_audiences" not in rec.adsets[0]["targeting"]
    assert r["audiences"]["visitors"] == "AUD1"


def test_cached_audiences_are_reused():
    cached = {**SETTINGS, "aud_pixel": "PX", "aud_account": "act_123", "aud_visitors": "V9", "aud_buyers": "B9"}
    L.launch_plan(PLAN_INPUTS, RT, cached, 7)
    assert Recorder.last.audiences == []
    assert Recorder.last.adsets[2]["targeting"]["custom_audiences"] == [{"id": "V9"}]
    # audiences made on another ad account are never used here: new ones are made
    r = L.launch_plan(PLAN_INPUTS, RT, {**cached, "aud_account": "act_999"}, 7)
    assert len(Recorder.last.audiences) == 2 and r["audiences"]["account"] == "act_123"


def test_no_audiences_drops_retargeting_and_keeps_total_budget():
    Recorder.audience_fail = True
    r = L.launch_plan(PLAN_INPUTS, RT, SETTINGS, 7)
    rec = Recorder.last
    assert [a["name"] for a in rec.adsets] == ["Broad", "Interest"]
    assert sum(a["daily_budget"] for a in rec.adsets) in (9999, 10000, 10001)
    assert any("Custom Audience" in n for n in r["notes"]) and any("Skipped ad set Retargeting" in n for n in r["notes"])
    assert all("excluded_custom_audiences" not in a["targeting"] for a in rec.adsets)


def test_no_pixel_no_audiences():
    r = L.launch_plan(PLAN_INPUTS, RT, {**SETTINGS, "meta_pixel_id": ""}, 7)
    assert Recorder.last.audiences == [] and len(Recorder.last.adsets) == 2
    assert any("No pixel" in n for n in r["notes"])


# ---------- carousels (the store makes no videos) ----------

def test_carousel_becomes_one_ad_with_cards():
    content = {**CONTENT, "creatives": [{**CONTENT["creatives"][0], "format": "Carousel"}]}
    imgs = [{"creative_index": 0, "image_hash": h} for h in ("H1", "H2", "H3")]
    r = L.launch_plan(PLAN_INPUTS, content, SETTINGS, 7, imgs)
    ld = Recorder.last.creatives[0]["object_story_spec"]["link_data"]
    assert [c["image_hash"] for c in ld["child_attachments"]] == ["H1", "H2", "H3"]
    assert all(c["link"] == SETTINGS["store_url"] and c["call_to_action"]["type"] == "SHOP_NOW" for c in ld["child_attachments"])
    assert "image_hash" not in ld and len(r["ads"]) == 2  # one carousel ad per ad set


def test_carousel_with_one_image_runs_as_single_image():
    content = {**CONTENT, "creatives": [{**CONTENT["creatives"][0], "format": "Carousel"}]}
    r = L.launch_plan(PLAN_INPUTS, content, SETTINGS, 7, [{"creative_index": 0, "image_hash": "ONLY"}])
    ld = Recorder.last.creatives[0]["object_story_spec"]["link_data"]
    assert ld["image_hash"] == "ONLY" and "child_attachments" not in ld
    assert any("carousel with one image" in n for n in r["notes"])


def test_carousel_notes_once_and_trim_warned():
    content = {**CONTENT, "creatives": [{**CONTENT["creatives"][0], "format": "Carousel"}]}
    r = L.launch_plan(PLAN_INPUTS, content, SETTINGS, 7, [{"creative_index": 0, "image_hash": "ONLY"}])
    assert sum("carousel with one image" in n for n in r["notes"]) == 1
    imgs = [{"creative_index": 0, "image_hash": f"H{k}"} for k in range(12)]
    r = L.launch_plan(PLAN_INPUTS, content, SETTINGS, 7, imgs)
    ld = Recorder.last.creatives[0]["object_story_spec"]["link_data"]
    assert len(ld["child_attachments"]) == 10 and "name" not in ld
    assert sum("first 10 of 12" in n for n in r["notes"]) == 1
