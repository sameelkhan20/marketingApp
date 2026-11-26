"""Fill the "Make it sell" brief from what the store already says: the text of its own Meta ads (the ones it
spent most on) and the product page (its description and the lines about delivery, returns, reviews, offers).

The AI only copies facts found in those texts; anything it cannot find stays empty. Nothing is invented,
because the planner treats the brief as the truth it may promise in ads."""

from __future__ import annotations

import html as html_lib
import re
from datetime import date, timedelta

import requests
from sqlalchemy.orm import Session

TOP_ADS = 8
_KEYWORDS = re.compile(r"\b(deliver\w*|dispatch\w*|shipping|ships?|returns?|refund\w*|exchange|reviews?|rated|stars?|"
                       r"customers?|free|% off|off\b|sale|discount|code|custom\w*|personali[sz]\w*|guarantee\w*|"
                       r"warranty|handmade|hand-?made|made in)\b", re.I)


def _ads_copy(vals: dict) -> list[str]:
    """Headline and text of the store's most-funded ads in the last two years."""
    from .meta_client import MetaClient, MetaError
    from .settings_store import ad_account_path
    if not vals.get("meta_access_token") or not vals.get("meta_ad_account_id"):
        return []
    client = MetaClient(vals["meta_access_token"])
    act = ad_account_path(vals["meta_ad_account_id"])
    until = date.today()
    rows = client.ad_insights(act, (until - timedelta(days=730)).isoformat(), until.isoformat())
    rows.sort(key=lambda r: -float(r.get("spend", 0) or 0))
    out, seen = [], set()
    for r in rows:
        if len(out) >= TOP_ADS:
            break
        try:
            c = client.ad_copy(r.get("ad_id", ""))
        except MetaError:
            continue
        text = " | ".join(x for x in (c.get("title"), c.get("body")) if x).strip()
        key = text.lower()[:120]
        if text and key not in seen:
            seen.add(key)
            out.append(text[:600])
    return out


def _page_facts(url: str) -> dict:
    """The product page: its description, and the visible lines about delivery, returns, reviews and offers."""
    if not url:
        return {}
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (compatible; AdsPlanner brief reader)"}, timeout=20)
    except requests.RequestException:
        return {}
    if r.status_code != 200:
        return {}
    page = r.text
    ld = _product_ld(page) or {}
    body = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", page)
    text = html_lib.unescape(re.sub(r"<[^>]+>", "\n", body))
    lines = []
    for line in (l.strip() for l in text.splitlines()):
        if 8 <= len(line) <= 220 and _KEYWORDS.search(line) and line not in lines:
            lines.append(line)
    return {"title": str(ld.get("name") or "")[:200],
            "description": html_lib.unescape(str(ld.get("description") or ""))[:1500],
            "lines": lines[:40]}


def _product_ld(html: str) -> dict | None:
    """The schema.org Product on a product page (JSON-LD), which most stores publish for Google."""
    import html as html_lib
    import json
    for block in re.findall(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', html, re.S | re.I):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        stack = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for it in stack:
            if not isinstance(it, dict):
                continue
            kind = it.get("@type")
            kinds = kind if isinstance(kind, list) else [kind]
            if "Product" in kinds or "ProductGroup" in kinds:
                it["name"] = html_lib.unescape(str(it.get("name") or ""))
                return it
    return None



SCHEMA = {
    "type": "object",
    "properties": {
        "offer": {"type": "string"},
        "usp": {"type": "string"},
        "delivery_days": {"type": "integer"},
        "returns": {"type": "string"},
        "social_proof": {"type": "string"},
        "what_your_ads_do": {"type": "string"},
    },
    "required": ["offer", "usp", "delivery_days", "returns", "social_proof", "what_your_ads_do"],
    "additionalProperties": False,
}

SYSTEM = ("You read a store's own ads and product page and pull out its selling facts for a Meta ads brief. "
          "Copy only facts stated in the texts; never invent a discount, a rating, a review count, a delivery "
          "time or a return period. Leave a field empty (\"\" or 0) when the texts do not state it.")


def suggest(session: Session, brand_id: int, landing_url: str = "") -> dict:
    from . import brands, llm
    from .meta_client import MetaError
    vals = brands.settings_for(session, brand_id)
    notes = []
    try:
        ads = _ads_copy(vals)
    except MetaError as ex:
        ads = []
        notes.append(f"Meta ads could not be read: {ex}")
    page = _page_facts(landing_url or vals.get("store_url", ""))
    if not ads and not page.get("description") and not page.get("lines"):
        raise ValueError("Nothing to read yet: connect the store to Meta, or pick a product so its page can be read.")
    if not llm.configured():
        raise ValueError("Filling the brief needs the AI key (GEMINI_API_KEY) on the server.")

    ads_text = "\n".join(f"- {a}" for a in ads) or "- (none found)"
    lines_text = "\n".join(f"- {l}" for l in page.get("lines", [])) or "- (none found)"
    prompt = f"""YOUR ADS (most money spent first)
{ads_text}

PRODUCT PAGE: {page.get('title', '')}
Description: {page.get('description', '') or '(none)'}
Lines about delivery, returns, reviews and offers:
{lines_text}

Fill:
- offer: the deal the ads or page state (discount, free shipping, free custom branding, bundle). "" if none.
- usp: why buy this one, in one short line built from concrete facts (material, thickness, handmade, made in...).
- delivery_days: the delivery time in days if stated (a range: use the upper number); 0 if not stated.
- returns: the return or refund promise if stated, e.g. "30-day returns"; "" if not.
- social_proof: reviews, ratings or customer counts if stated; "" if not.
- what_your_ads_do: 2 short sentences for the owner: which offer, angle and words their ads keep using.
Keep every field under 200 characters."""
    out = llm.structured(SYSTEM, prompt, SCHEMA, "Brief from ads", effort="low", max_tokens=1500)
    clean = {k: (str(out.get(k) or "").strip()[:200]) for k in ("offer", "usp", "returns", "social_proof")}
    try:
        days = int(out.get("delivery_days") or 0)
    except (TypeError, ValueError):
        days = 0
    clean["delivery_days"] = days if 0 < days <= 90 else 0
    clean["what_your_ads_do"] = str(out.get("what_your_ads_do") or "").strip()[:400]
    clean["read"] = {"ads": len(ads), "page": bool(page.get("description") or page.get("lines"))}
    clean["notes"] = notes
    return clean
