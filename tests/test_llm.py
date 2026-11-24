"""Gemini text provider: JSON parsing, retry on a bad answer, usage recording, and fallbacks."""

import json

import pytest

from backend import learning, llm, monitor, outcomes, planner, usage
from backend.db import ApiUsage, get_session
from backend.estimator import estimate_orders, estimate_to_dict


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, json.dumps(body)

    def json(self):
        return self._body


def _gemini_reply(obj, fence=False):
    text = json.dumps(obj)
    if fence:
        text = "```json\n" + text + "\n```"
    return _Resp(200, {"candidates": [{"content": {"parts": [{"text": text}]}}],
                       "usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 200}})


@pytest.fixture
def gemini(monkeypatch):
    monkeypatch.setattr(llm, "GEMINI_API_KEY", "AIza-test")
    monkeypatch.setattr(llm, "ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(llm, "LLM_PROVIDER", "")
    usage.set_budget(0)
    calls = []

    def install(*replies):
        it = iter(replies)

        def post(url, headers=None, json=None, timeout=None):
            calls.append({"url": url, "headers": headers, "json": json})
            return next(it)
        monkeypatch.setattr(llm.requests, "post", post)
        return calls
    return install


def test_provider_selection(monkeypatch):
    monkeypatch.setattr(llm, "LLM_PROVIDER", "")
    monkeypatch.setattr(llm, "ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(llm, "GEMINI_API_KEY", "")
    assert not llm.configured()
    monkeypatch.setattr(llm, "GEMINI_API_KEY", "AIza-x")
    assert llm.provider() == "gemini" and llm.configured()
    monkeypatch.setattr(llm, "ANTHROPIC_API_KEY", "sk-x")
    assert llm.provider() == "anthropic"
    monkeypatch.setattr(llm, "LLM_PROVIDER", "gemini")
    assert llm.provider() == "gemini"


def test_structured_parses_and_records(gemini):
    calls = gemini(_gemini_reply({"rules": ["a"]}, fence=True))
    out = llm.structured("sys", "p", {"type": "object", "required": ["rules"]}, "Learning")
    assert out == {"rules": ["a"]}
    assert calls[0]["headers"]["x-goog-api-key"] == "AIza-test"
    assert calls[0]["json"]["generationConfig"]["responseMimeType"] == "application/json"
    with get_session() as s:
        row = s.query(ApiUsage).order_by(ApiUsage.id.desc()).first()
        assert (row.provider, row.input_tokens, row.output_tokens) == ("gemini", 1000, 200) and row.cost_usd > 0


def test_retries_once_when_keys_missing(gemini):
    calls = gemini(_gemini_reply({"wrong": 1}), _gemini_reply({"rules": []}))
    assert llm.structured("s", "p", {"required": ["rules"]}, "Learning") == {"rules": []}
    assert len(calls) == 2
    gemini(_gemini_reply({"x": 1}), _Resp(200, {"candidates": [{"content": {"parts": [{"text": "not json"}]}}]}))
    with pytest.raises(llm.LLMError):
        llm.structured("s", "p", {"required": ["rules"]}, "Learning")


def test_http_error_raises(gemini):
    gemini(_Resp(429, {"error": {"message": "quota"}}))
    with pytest.raises(llm.LLMError, match="quota"):
        llm.structured("s", "p", {"required": []}, "Plans")


def test_plan_falls_back_to_template_on_failure(gemini):
    gemini(_Resp(500, {"error": {"message": "down"}}))
    inputs = {"budget_pkr": 100000, "country": "US", "product": "Jacket", "niche": "leather_jackets",
              "price_usd": 120, "duration_days": 30}
    plan = planner.generate_plan(inputs, estimate_to_dict(estimate_orders(100000, "US")), [])
    assert "failed" in plan["risks"][0] and plan["ad_sets"]


def test_plan_uses_gemini_answer(gemini):
    inputs = {"budget_pkr": 100000, "country": "US", "product": "Jacket", "niche": "leather_jackets",
              "price_usd": 120, "duration_days": 30}
    fake_plan = planner._template_plan(inputs, estimate_to_dict(estimate_orders(100000, "US")), [])
    fake_plan["summary"] = "from gemini"
    broken = json.loads(json.dumps(fake_plan))
    del broken["ad_sets"][0]["daily_budget_pct"]
    reviewed = json.loads(json.dumps(fake_plan))
    reviewed["summary"] = "reviewed"
    reviewed["risks"] = reviewed["risks"] + ["Review: merged ad sets for the small budget"]
    calls = gemini(_gemini_reply(broken), _gemini_reply(fake_plan), _gemini_reply(reviewed))
    out = planner.generate_plan(inputs, estimate_to_dict(estimate_orders(100000, "US")), [])
    assert out["summary"] == "reviewed" and any(r.startswith("Review:") for r in out["risks"])
    assert len(calls) == 3  # draft (asked twice: a nested field was missing), then the review
    assert all("gemini-pro-latest" in c["url"] for c in calls)  # plans use the stronger model
    assert "REVIEW" in calls[2]["json"]["systemInstruction"]["parts"][0]["text"].upper()


def test_review_failure_keeps_the_draft(gemini):
    inputs = {"budget_pkr": 100000, "country": "US", "product": "Jacket", "niche": "leather_jackets",
              "price_usd": 120, "duration_days": 30, "offer": "Free shipping"}
    draft = planner._template_plan(inputs, estimate_to_dict(estimate_orders(100000, "US")), [])
    draft["summary"] = "draft"
    gemini(_gemini_reply(draft), _Resp(500, {"error": {"message": "busy"}}))
    out = planner.generate_plan(inputs, estimate_to_dict(estimate_orders(100000, "US")), [])
    assert out["summary"] == "draft" and any("Review step skipped" in r for r in out["risks"])


def test_brief_reaches_the_prompt_and_is_not_invented():
    inputs = {"budget_pkr": 100000, "country": "US", "product": "Jacket", "niche": "leather_jackets",
              "price_usd": 120, "duration_days": 30, "offer": "20% off this week", "delivery_days": 9}
    text = planner._user_prompt(inputs, estimate_to_dict(estimate_orders(100000, "US")), [])
    assert "Offer: 20% off this week" in text and "Delivery time (days): 9" in text
    empty = planner._user_prompt({**inputs, "offer": "", "delivery_days": 0}, estimate_to_dict(estimate_orders(100000, "US")), [])
    assert "do not promise discounts" in empty
    plan = planner._template_plan(inputs, estimate_to_dict(estimate_orders(100000, "US")), [])
    assert plan["creatives"][0]["angle"] == "offer" and "20% off this week" in plan["creatives"][0]["hook"]
    assert len({c["angle"] for c in plan["creatives"]}) == len(plan["creatives"])


def test_missing_checks_nested():
    sch = {"type": "object", "required": ["a"], "properties": {"a": {"type": "array", "items": {
        "type": "object", "required": ["x"], "properties": {"x": {"type": "string"}}}}}}
    assert llm._missing(sch, {"a": [{"x": "1"}, {}]}) == ["a[1].x"]
    assert llm._missing(sch, {"a": "no"}) == ["a"]
    assert llm._missing(sch, {"a": []}) == []


def test_learning_uses_gemini_and_falls_back(gemini):
    gemini(_gemini_reply({"rules": ["Keep retargeting under 20% of budget.", " "]}))
    assert learning._extract_rules(["ad_sets changed"], "") == ["Keep retargeting under 20% of budget."]
    gemini(_Resp(500, {}))
    assert learning._extract_rules(["x"], "my note") == ["my note"]
