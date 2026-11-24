"""The store link the ads send people to: a broken or very slow page wastes every click paid for.
Checked once before launch (readiness, live only). Never blocks on our own network trouble."""

from __future__ import annotations

import os
import time

import requests

ENABLED = os.getenv("STORE_CHECK", "1").lower() not in ("0", "false", "no")
SLOW_SECONDS = 4.0


def check(url: str) -> list[dict]:
    """[{key, ok, text, hard}] for the ad link."""
    if not ENABLED or not url:
        return []
    out = []
    t0 = time.monotonic()
    try:
        r = requests.get(url, timeout=12, allow_redirects=True,
                         headers={"User-Agent": "Mozilla/5.0 (compatible; AdsPlanner link check)"})
    except requests.RequestException as ex:
        return [{"key": "store_page", "ok": False, "hard": False,
                 "text": f"Could not open the store link ({type(ex).__name__}). Open it yourself before launching: "
                         "a broken link wastes every click"}]
    secs = time.monotonic() - t0
    if r.status_code >= 400:
        gone = r.status_code in (404, 410)  # 403/429/503 are often bot protection: a warning, not a block
        out.append({"key": "store_page", "ok": False, "hard": gone,
                    "text": f"The store link returns error {r.status_code}" + (
                        ": the page does not exist, so every click would be wasted. Fix the link under Stores" if gone
                        else ". It may only be blocking automatic checks: open it on your phone before launching")})
        return out
    if not r.url.lower().startswith("https://"):
        out.append({"key": "store_https", "ok": False, "hard": False,
                    "text": "The store link is not HTTPS: browsers show 'Not secure' and fewer people pay"})
    if secs > SLOW_SECONDS:
        out.append({"key": "store_speed", "ok": False, "hard": False,
                    "text": f"The store page took {secs:.1f}s to load: many mobile buyers leave before it opens. "
                            "Compress images and remove unused apps or plugins"})
    html = r.text[:400_000].lower()
    shopify = "cdn.shopify.com" in html or "shopify" in r.headers.get("x-shopid", "").lower() or r.headers.get("x-shopid")
    if not shopify and "connect.facebook.net" not in html and "fbq(" not in html:
        # Shopify's Meta app loads the pixel in a sandbox that is not in the page source: only check other stores
        out.append({"key": "store_pixel", "ok": False, "hard": False,
                    "text": "No Meta pixel code found on the store page. On WooCommerce install the official "
                            "'Facebook for WooCommerce' plugin (it also sends server events)"})
    if not out:
        out.append({"key": "store_page", "ok": True, "hard": False, "text": f"Store link opens ({secs:.1f}s)"})
    return out
