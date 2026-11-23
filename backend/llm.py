"""Text AI behind one call: Claude when ANTHROPIC_API_KEY is set, otherwise Gemini (the Social
Agent's Google key works). LLM_PROVIDER=anthropic|gemini forces one.

structured(system, prompt, schema, feature) -> dict
  Claude: structured outputs (the JSON is guaranteed to match the schema).
  Gemini: JSON mode with the schema in the prompt (as the Social Agent does), then the required
  keys are checked and the call is retried once.
Every call checks the monthly AI budget first and records its tokens on the Payments page.
"""

import json
import logging

import requests

from . import usage
from .config import ANTHROPIC_API_KEY, CLAUDE_MODEL, GEMINI_API_KEY, GEMINI_MODEL, LLM_PROVIDER

log = logging.getLogger("llm")
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
_NUMERIC_TEXT = __import__("re").compile(r"^\s*[$-]?\s*-?[\d,]*\.?\d+\s*%?\s*$")  # "40", "40%", "$45", "1,200"


class LLMError(RuntimeError):
    pass


def provider() -> str:
    if LLM_PROVIDER in ("anthropic", "gemini"):
        return LLM_PROVIDER
    if ANTHROPIC_API_KEY:
        return "anthropic"
    return "gemini" if GEMINI_API_KEY else ""


def configured() -> bool:
    p = provider()
    return (p == "anthropic" and bool(ANTHROPIC_API_KEY)) or (p == "gemini" and bool(GEMINI_API_KEY))


def model() -> str:
    return {"anthropic": CLAUDE_MODEL, "gemini": GEMINI_MODEL}.get(provider(), "")


def structured(system: str, prompt: str, schema: dict, feature: str, effort: str = "medium",
               max_tokens: int = 16000) -> dict:
    """Raises usage.BudgetExceeded when the monthly budget is used up, LLMError on any other failure."""
    if not configured():
        raise LLMError("No AI provider: set ANTHROPIC_API_KEY or a Gemini key in .env")
    usage.ensure_budget(feature.lower())
    if provider() == "anthropic":
        return _claude(system, prompt, schema, feature, effort, max_tokens)
    return _gemini(system, prompt, schema, feature, max_tokens)


# ---------------- Claude ----------------

def _claude(system: str, prompt: str, schema: dict, feature: str, effort: str, max_tokens: int) -> dict:
    import anthropic

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    try:
        with client.beta.messages.stream(
            model=CLAUDE_MODEL, max_tokens=max_tokens,
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            system=system,
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            response = stream.get_final_message()
    except anthropic.APIError as ex:
        usage.record("anthropic", CLAUDE_MODEL, feature, ok=False, error=str(ex))
        raise LLMError(f"Claude error: {ex}") from ex
    usage.record_claude(response, feature)
    if response.stop_reason == "refusal":
        raise LLMError("Claude declined this request")
    if response.stop_reason == "max_tokens":
        raise LLMError("Claude's answer was cut off (max_tokens)")
    try:
        return json.loads(next(b.text for b in response.content if b.type == "text"))
    except (StopIteration, ValueError) as ex:
        raise LLMError("Claude returned no JSON") from ex


# ---------------- Gemini ----------------

def _gemini(system: str, prompt: str, schema: dict, feature: str, max_tokens: int) -> dict:
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": prompt + "\n\nAnswer with one JSON object only, matching "
                                                 "this JSON Schema exactly (every required key present):\n"
                                                 + json.dumps(schema)}]}],
        "generationConfig": {"responseMimeType": "application/json", "maxOutputTokens": max_tokens,
                             "temperature": 0.4},
    }
    last = ""
    for attempt in range(2):
        if attempt:
            usage.ensure_budget(feature.lower())  # the retry is billed too
        try:
            r = requests.post(f"{GEMINI_BASE}/models/{GEMINI_MODEL}:generateContent",
                              headers={"x-goog-api-key": GEMINI_API_KEY}, json=body, timeout=120)
        except requests.RequestException as ex:
            raise LLMError(f"Gemini network error: {ex}") from ex
        try:
            data = r.json()
        except ValueError:
            data = {}
        meta = data.get("usageMetadata") or {}
        usage.record("gemini", GEMINI_MODEL, feature, int(meta.get("promptTokenCount") or 0),
                     int(meta.get("candidatesTokenCount") or 0) + int(meta.get("thoughtsTokenCount") or 0),
                     ok=r.status_code < 400, error="" if r.status_code < 400 else r.text[:300])
        if r.status_code >= 400:
            msg = (data.get("error") or {}).get("message") if isinstance(data.get("error"), dict) else r.text[:200]
            raise LLMError(f"Gemini error {r.status_code}: {msg}")
        text = "".join(p.get("text", "") for c in data.get("candidates") or []
                       for p in (c.get("content") or {}).get("parts") or [] if not p.get("thought"))
        finish = ((data.get("candidates") or [{}])[0] or {}).get("finishReason", "")
        if finish == "MAX_TOKENS":  # cut off (thinking counts too): try once more with room to finish
            body["generationConfig"]["maxOutputTokens"] = min(max_tokens * 2, 65536)
            last = "Gemini's answer was cut off"
            continue
        try:
            out = json.loads(_strip_fence(text))
        except ValueError:
            last = "Gemini returned text that is not JSON"
            continue
        missing = _missing(schema, out)
        if not missing:
            return out
        last = f"Gemini's answer is missing {', '.join(missing[:5])}"
        log.warning("%s; retrying once", last)
    raise LLMError(last)


def _missing(schema: dict, value, path: str = "") -> list[str]:
    """Required keys absent anywhere in the answer (objects, and objects inside arrays), and values of
    the wrong kind (text where a number belongs), so the call is retried instead of saving bad data."""
    t = schema.get("type")
    if t in ("number", "integer"):
        ok = (isinstance(value, (int, float)) and not isinstance(value, bool)) or \
            (isinstance(value, str) and _NUMERIC_TEXT.match(value) is not None)
        return [] if ok else [f"{path.rstrip('.') or 'value'} (not a number)"]
    if t == "string":
        return [] if isinstance(value, str) else [f"{path.rstrip('.') or 'value'} (not text)"]
    if t == "object" or "properties" in schema or "required" in schema:
        if not isinstance(value, dict):
            return [path or "the JSON object"]
        out = [f"{path}{k}" for k in schema.get("required", []) if k not in value]
        for k, sub in (schema.get("properties") or {}).items():
            if k in value and isinstance(sub, dict):
                out += _missing(sub, value[k], f"{path}{k}.")
        return out
    if t == "array" and isinstance(schema.get("items"), dict):
        if not isinstance(value, list):
            return [path.rstrip(".") or "array"]
        out = []
        for i, v in enumerate(value):
            out += _missing(schema["items"], v, f"{path.rstrip('.')}[{i}].")
        return out
    return []


def _strip_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        t = t.rsplit("```", 1)[0]
    return t.strip()
