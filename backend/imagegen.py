"""Image generation, same approach as the Social Agent.

Providers:
- higgsfield (default): the team's plan credits through Higgsfield's MCP server (see higgsfield.py);
  connected once on the Connections page, product photo passed via a temporary public R2 URL.
- gemini: POST {GEMINI_BASE}/models/{model}:generateContent with responseModalities=IMAGE.
  The product photo goes in as an inline_data part so the jacket stays exactly as photographed;
  the image comes back as inline base64.
- openai: gpt-image-1 (edits endpoint when a product photo is given).

generate(prompt, reference=None, aspect="1:1") -> image bytes. Errors raise ImageGenError.
"""

import base64
import mimetypes

import requests

from .config import HIGGSFIELD_MODEL, IMAGE_API_KEY, IMAGE_MODEL, IMAGE_PROVIDER

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODELS = {"gemini": "gemini-3.1-flash-image", "openai": "gpt-image-1"}
OPENAI_SIZES = {"1:1": "1024x1024", "9:16": "1024x1536", "4:5": "1024x1536"}

# Always appended (as in the Social Agent): generators sometimes invent logos, text or faces,
# and Meta rejects ads for trademark or likeness.
SAFE_SUFFIX = ("High-quality realistic photograph. No text, no letters, no captions, no watermarks, no logos, "
               "no brand names or brand marks, no celebrities or recognizable real people.")
REFERENCE_RULE = ("The attached photo is the PRODUCT. Show exactly this garment: keep its colour, material, cut, "
                  "collar, zips, buttons, stitching and every visible detail unchanged. Take only the garment from "
                  "the photo; ignore its background, the person wearing it and any text or watermark.")


class ImageGenError(Exception):
    pass


def _model() -> str:
    return IMAGE_MODEL or DEFAULT_MODELS.get(IMAGE_PROVIDER, "")


def status() -> dict:
    if IMAGE_PROVIDER == "higgsfield":
        from . import higgsfield
        connected = higgsfield.status()["connected"]
        return {"provider": "higgsfield" if connected else "off", "model": HIGGSFIELD_MODEL if connected else "",
                "configured": connected,
                **({} if connected else {"hint": "Connect Higgsfield on the Connections page"})}
    configured = IMAGE_PROVIDER in DEFAULT_MODELS and bool(IMAGE_API_KEY)
    return {"provider": IMAGE_PROVIDER if configured else "off", "model": _model() if configured else "",
            "configured": configured}


def generate(prompt: str, reference: str | None = None, aspect: str = "1:1") -> bytes:
    if not status()["configured"]:
        raise ImageGenError("Higgsfield is not connected: connect it on the Connections page." if IMAGE_PROVIDER == "higgsfield"
                            else "Image generation is not configured. Set IMAGE_PROVIDER and IMAGE_API_KEY in .env.")
    from . import usage
    try:
        usage.ensure_budget("image generation")
    except usage.BudgetExceeded as ex:
        raise ImageGenError(str(ex))
    prompt = f"{prompt.strip().rstrip('.')}. {SAFE_SUFFIX}"
    try:
        data = _make(prompt, reference, aspect)
    except ImageGenError as ex:
        usage.record(IMAGE_PROVIDER, status()["model"], "Ad images", images=1 if "credits" in str(ex) else 0,
                     ok=False, error=str(ex))
        raise
    usage.record(IMAGE_PROVIDER, status()["model"], "Ad images", images=1)
    return data


def _make(prompt: str, reference: str | None, aspect: str) -> bytes:
    if IMAGE_PROVIDER == "higgsfield":
        return _higgsfield(prompt, reference, aspect)
    if IMAGE_PROVIDER == "gemini":
        return _gemini(prompt, reference, aspect)
    return _openai(prompt, reference, aspect)


def _higgsfield(prompt: str, reference: str | None, aspect: str) -> bytes:
    from . import higgsfield
    ref = None
    if reference:
        with open(reference, "rb") as f:
            ref = (f.read(), mimetypes.guess_type(reference)[0] or "image/png")
        prompt = f"{REFERENCE_RULE}\n{prompt}"
    try:
        return higgsfield.generate_with_reference(prompt, ref, aspect)
    except higgsfield.HiggsfieldError as ex:
        msg = str(ex) + (" (credits for this picture were spent)" if ex.charged else "")
        raise ImageGenError(msg) from ex


def _gemini(prompt: str, reference: str | None, aspect: str) -> bytes:
    parts = []
    if reference:
        mime = mimetypes.guess_type(reference)[0] or "image/png"
        with open(reference, "rb") as f:
            parts.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(f.read()).decode()}})
        prompt = f"{REFERENCE_RULE}\n{prompt}"
    parts.append({"text": prompt})
    body = {"contents": [{"parts": parts}],
            "generationConfig": {"responseModalities": ["IMAGE"], "imageConfig": {"aspectRatio": aspect}}}
    try:
        r = requests.post(f"{GEMINI_BASE}/models/{_model()}:generateContent",
                          headers={"x-goog-api-key": IMAGE_API_KEY}, json=body, timeout=180)
    except requests.RequestException as ex:
        raise ImageGenError(f"Network error: {ex}")
    if r.status_code >= 400:
        raise ImageGenError(_err_text(r))
    data = r.json()
    for cand in data.get("candidates") or []:
        for part in (cand.get("content") or {}).get("parts") or []:
            inline = part.get("inlineData") or part.get("inline_data")
            if inline and inline.get("data"):
                return base64.b64decode(inline["data"])
    # No image: say why (safety block, finish reason) so the user can rephrase.
    cands = data.get("candidates") or []
    reason = (data.get("promptFeedback") or {}).get("blockReason") or (cands[0].get("finishReason") if cands else "")
    raise ImageGenError(f"Gemini returned no image ({reason or 'unknown reason'}). Try rephrasing the prompt.")


def _openai(prompt: str, reference: str | None, aspect: str) -> bytes:
    headers = {"Authorization": f"Bearer {IMAGE_API_KEY}"}
    size = OPENAI_SIZES.get(aspect, "1024x1024")
    try:
        if reference:
            with open(reference, "rb") as f:
                r = requests.post("https://api.openai.com/v1/images/edits", headers=headers,
                                  data={"model": _model(), "prompt": f"{REFERENCE_RULE}\n{prompt}", "size": size, "n": 1},
                                  files={"image[]": (reference, f, mimetypes.guess_type(reference)[0] or "image/png")},
                                  timeout=180)
        else:
            r = requests.post("https://api.openai.com/v1/images/generations", headers=headers,
                              json={"model": _model(), "prompt": prompt, "size": size, "n": 1, "quality": "medium"},
                              timeout=180)
    except requests.RequestException as ex:
        raise ImageGenError(f"Network error: {ex}")
    if r.status_code >= 400:
        raise ImageGenError(_err_text(r))
    try:
        return base64.b64decode(r.json()["data"][0]["b64_json"])
    except (KeyError, IndexError, ValueError):
        raise ImageGenError("OpenAI returned no image data")


def _err_text(r) -> str:
    try:
        body = r.json()
        err = body.get("error")
        msg = err.get("message") if isinstance(err, dict) else err or body.get("detail")
    except ValueError:
        msg = None
    return f"{IMAGE_PROVIDER} error {r.status_code}: {msg or r.text[:200]}"
