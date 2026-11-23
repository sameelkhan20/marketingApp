"""Activity log and Payments (AI usage, monthly budget, ad spend)."""

from types import SimpleNamespace

import pytest

from backend import imagegen, planner, usage
from backend.db import ActivityLog, ApiUsage, get_session
from tests.conftest import OWNER


@pytest.fixture(autouse=True)
def _clean_usage():
    with get_session() as s:
        s.query(ApiUsage).delete()
        s.commit()
    usage.set_budget(usage.DEFAULT_BUDGET_USD)
    usage.set_prices({})
    yield


def test_cost_from_price_table():
    assert usage.cost("anthropic", "claude-opus-5-5", 1_000_000, 100_000) == pytest.approx(4.0 + 2.0)
    assert usage.cost("anthropic", "some-new-model", 500_000, 0) == pytest.approx(2.0)  # provider:* fallback
    assert usage.cost("gemini", "x", images=10) == pytest.approx(0.39)
    usage.set_prices({"higgsfield:*": {"image": 0.05}})
    assert usage.cost("higgsfield", "gpt_image_2_5", images=4) == pytest.approx(0.20)


def test_record_claude_counts_cache_tokens():
    resp = SimpleNamespace(model="claude-opus-5-5", usage=SimpleNamespace(
        input_tokens=1000, output_tokens=500, cache_read_input_tokens=2000, cache_creation_input_tokens=0))
    usage.record_claude(resp, "Plans")
    with get_session() as s:
        row = s.query(ApiUsage).one()
        assert row.input_tokens == 3000 and row.feature == "Plans" and row.cost_usd > 0


def test_budget_stops_ai_work(monkeypatch):
    usage.set_budget(1.0)
    usage.record("anthropic", "claude-opus-5-5", "Plans", 300_000, 0)  # $1.20
    with pytest.raises(usage.BudgetExceeded):
        usage.ensure_budget("plan generation")
    # plans fall back to the template and say why
    from backend import llm
    monkeypatch.setattr(llm, "GEMINI_API_KEY", "AIza-test")
    inputs = {"budget_pkr": 100000, "country": "US", "product": "Jacket", "niche": "leather_jackets",
              "price_usd": 120, "duration_days": 30}
    from backend.estimator import estimate_orders, estimate_to_dict
    plan = planner.generate_plan(inputs, estimate_to_dict(estimate_orders(100000, "US")), [])
    assert "budget" in plan["risks"][0].lower()
    # images are refused before any provider is called
    monkeypatch.setattr(imagegen, "status", lambda: {"configured": True, "provider": "gemini", "model": "m"})
    with pytest.raises(imagegen.ImageGenError, match="budget"):
        imagegen.generate("x")
    usage.set_budget(0)  # 0 = no limit
    usage.ensure_budget("anything")


def test_image_success_and_failure_recorded(monkeypatch):
    monkeypatch.setattr(imagegen, "status", lambda: {"configured": True, "provider": "gemini", "model": "m"})
    monkeypatch.setattr(imagegen, "IMAGE_PROVIDER", "gemini")
    monkeypatch.setattr(imagegen, "_make", lambda *a: b"IMG")
    imagegen.generate("x")

    def boom(*a):
        raise imagegen.ImageGenError("refused")
    monkeypatch.setattr(imagegen, "_make", boom)
    with pytest.raises(imagegen.ImageGenError):
        imagegen.generate("x")
    with get_session() as s:
        rows = s.query(ApiUsage).order_by(ApiUsage.id).all()
        assert [(r.images, bool(r.ok)) for r in rows] == [(1, True), (0, False)]


def test_billing_api(client, auth):
    usage.record("anthropic", "claude-opus-5-5", "Plans", 100_000, 10_000)
    b = client.get("/api/billing", headers=auth).json()
    assert b["calls"] == 1 and b["spent_usd"] > 0 and b["by_feature"][0]["feature"] == "Plans"
    assert "ads" in b and "ad_spend_usd" in b and b["prices"]["anthropic:*"]["in"] == 4.0
    r = client.post("/api/billing", headers=auth, json={"budget_usd": 50, "prices": {"anthropic:*": {"in": 3, "out": 15}}})
    assert r.status_code == 200 and r.json()["budget_usd"] == 50 and r.json()["prices"]["anthropic:*"]["in"] == 3
    assert client.post("/api/billing", headers=auth, json={"budget_usd": -1}).status_code == 422


def test_activity_records_changes_and_logins(client, auth):
    with get_session() as s:
        before = s.query(ActivityLog).count()
    client.post("/api/auth/login", json={**OWNER, "password": "wrong-password"})
    client.post("/api/rules", headers=auth, json={"rule": "Always test two hooks"})
    client.post("/api/plans/999999/approve", headers=auth)  # 404 -> recorded as a failed action
    items = client.get("/api/activity", headers=auth).json()["items"]
    titles = [i["title"] for i in items[:6]]
    assert any(t == "Failed sign-in" for t in titles)
    assert any(t == "Rule added by hand" for t in titles)
    failed = next(i for i in items if i["title"].startswith("Plan #999999 approved"))
    assert failed["level"] == "warning" and failed["title"].endswith("failed") and failed["actor"] == OWNER["email"]
    with get_session() as s:
        assert s.query(ActivityLog).count() >= before + 3  # failed sign-in, rule, failed approve
    # filters and paging
    only_auth = client.get("/api/activity?kind=auth", headers=auth).json()["items"]
    assert only_auth and all(i["kind"] == "auth" for i in only_auth)
    found = client.get("/api/activity?q=two%20hooks", headers=auth).json()["items"]
    assert found and all("two hooks" in (i["title"] + i["detail"]).lower() or i["title"] == "Rule added by hand" for i in found)
    page = client.get("/api/activity?limit=1", headers=auth).json()
    older = client.get(f"/api/activity?limit=1&before_id={page['items'][0]['id']}", headers=auth).json()
    assert older["items"][0]["id"] < page["items"][0]["id"]


def test_reads_are_not_logged(client, auth):
    with get_session() as s:
        before = s.query(ActivityLog).count()
    client.get("/api/plans", headers=auth)
    client.post("/api/estimate", headers=auth, json={"budget_pkr": 100000, "country": "US", "product": "x jacket",
                                                     "niche": "leather_jackets", "price_usd": 120, "duration_days": 30})
    with get_session() as s:
        assert s.query(ActivityLog).count() == before


def test_monitor_run_records_activity():
    from backend import monitor
    with get_session() as s:
        before = s.query(ActivityLog).count()
    monitor._record_run({"checked": [{"x": 1}], "errors": [], "new_alerts": 2,
                         "auto_actions": [{"ok": True, "description": "Pause adset A1", "error": ""}],
                         "email": {"sent": 2}})
    with get_session() as s:
        new = s.query(ActivityLog).order_by(ActivityLog.id.desc()).limit(3).all()
        assert s.query(ActivityLog).count() == before + 3
        assert {a.actor for a in new} == {"monitor"}
    monitor._record_run({"checked": [], "errors": []})  # quiet run: nothing logged
    with get_session() as s:
        assert s.query(ActivityLog).count() == before + 3


def test_activity_api_needs_login(client):
    assert client.get("/api/activity").status_code == 401
    assert client.get("/api/billing").status_code == 401
