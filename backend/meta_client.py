"""Thin Meta Marketing API (Graph API) client.

Only the calls this app needs. Every call raises MetaError with Meta's own
message so the UI can show exactly what Meta complained about.
"""

import json
from typing import Any

import requests

from .config import META_API_VERSION

BASE = f"https://graph.facebook.com/{META_API_VERSION}"


class MetaError(Exception):
    pass


class MetaRateLimited(MetaError):
    """Meta asked us to slow down. Every call from this server waits until the pause is over, so the
    app never keeps hammering an account that is being throttled (which is what gets API access blocked)."""


# Throttle error codes: app / user / ad account / business use-case limits.
THROTTLE_CODES = {4, 17, 32, 613, 80000, 80001, 80002, 80003, 80004, 80005, 80006, 80008, 80009, 80014}
SLOW_DOWN_AT_PCT = 85        # pause before Meta has to step in
ABUSE_CODES = {368}          # "deemed abusive or otherwise disallowed": a temporary policy block, wait hours
_pause = {"until": 0.0, "why": ""}


def _load_pause() -> None:
    """A pause outlives a restart (it is kept in the settings table)."""
    if _pause.get("loaded"):
        return
    _pause["loaded"] = True
    try:
        from .db import Setting, get_session
        with get_session() as s:
            row = s.get(Setting, "meta_pause")
            if row and row.value:
                d = json.loads(row.value)
                _pause.update(until=float(d.get("until", 0)), why=str(d.get("why", "")))
    except Exception:
        pass


def _save_pause() -> None:
    try:
        from .db import Setting, get_session
        with get_session() as s:
            row = s.get(Setting, "meta_pause")
            val = json.dumps({"until": _pause["until"], "why": _pause["why"]})
            if row:
                row.value = val
            else:
                s.add(Setting(key="meta_pause", value=val))
            s.commit()
    except Exception:
        pass


def paused() -> tuple[bool, str]:
    import time
    _load_pause()
    left = _pause["until"] - time.time()
    return (left > 0, f"{_pause['why']} The app waits {int(left // 60) + 1} more minute(s) before calling Meta again."
            if left > 0 else "")


def _pause_for(seconds: float, why: str) -> None:
    import time
    _load_pause()
    until = time.time() + max(60.0, min(seconds, 6 * 3600))
    if until > _pause["until"]:
        _pause.update(until=until, why=why)
        _save_pause()


def _read_usage(headers) -> None:
    """Meta reports how much of each limit is used in these headers; slow down before it is reached."""
    for name in ("x-business-use-case-usage", "x-ad-account-usage", "x-app-usage", "x-fb-ads-insights-throttle"):
        raw = headers.get(name) if headers else None
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        entries = []
        if isinstance(data, dict) and all(isinstance(v, list) for v in data.values()):
            for v in data.values():
                entries += [e for e in v if isinstance(e, dict)]
        elif isinstance(data, dict):
            entries = [data]
        for e in entries:
            pct = max([float(e.get(k, 0) or 0) for k in ("call_count", "total_cputime", "total_time",
                                                        "acc_id_util_pct", "app_id_util_pct")] + [0.0])
            wait_min = float(e.get("estimated_time_to_regain_access", 0) or 0)
            if wait_min > 0:
                _pause_for(wait_min * 60, "Meta has paused this app's access for a while.")
            elif pct >= SLOW_DOWN_AT_PCT:
                _pause_for(15 * 60, f"Meta usage is at {int(pct)}% of its limit.")


def scrub(text: str) -> str:
    """Remove access tokens from any text that may reach the UI, DB or logs."""
    import re
    return re.sub(r"(access_token=)[^&\s'\"]+", r"\1***", re.sub(r"EAA[A-Za-z0-9]{20,}", "EAA***", text))


class MetaClient:
    def __init__(self, access_token: str):
        if not access_token:
            raise MetaError("Meta access token is not set. Open Connections and connect first.")
        self.token = access_token

    # ---------- low level ----------

    def _call(self, method: str, path: str, _force: bool = False, **params: Any) -> dict:
        """_force: the one cleanup call (deleting a half-built campaign) may go out during a pause."""
        params = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
                  for k, v in params.items() if v is not None}
        # Token goes in a header, never in the URL, so it cannot leak into error text or logs.
        headers = {"Authorization": f"Bearer {self.token}"}
        is_paused, why = paused()
        if is_paused and not _force:
            raise MetaRateLimited(why)
        try:
            if method == "GET":
                r = requests.get(f"{BASE}/{path}", params=params, headers=headers, timeout=60)
            else:
                r = requests.post(f"{BASE}/{path}", data=params, headers=headers, timeout=60)
        except requests.RequestException as ex:
            raise MetaError(f"Network error talking to Meta: {scrub(str(ex))}")
        _read_usage(getattr(r, "headers", None))
        try:
            data = r.json() if r.content else {}
        except ValueError:
            raise MetaError(f"Meta returned a non-JSON response ({r.status_code}): {scrub(r.text[:200])}")
        if r.status_code >= 400 or "error" in data:
            err = data.get("error", {}) if isinstance(data.get("error"), dict) else {}
            msg = err.get("error_user_msg") or err.get("message") or r.text
            code = err.get("code", r.status_code)
            if code in ABUSE_CODES:
                _pause_for(6 * 3600, "Meta flagged an action as abusive or disallowed.")
                raise MetaRateLimited(f"Meta blocked an action ({code}): {scrub(str(msg))}. The app stops calling Meta "
                                      "for 6 hours. Check Business Support Home for warnings on this account.")
            if code in THROTTLE_CODES or err.get("is_transient") and r.status_code == 429:
                _pause_for(30 * 60, "Meta asked the app to slow down (rate limit).")
                raise MetaRateLimited(f"Meta asked the app to slow down ({code}): {scrub(str(msg))}. "
                                      "The app pauses its Meta calls for 30 minutes.")
            raise MetaError(f"Meta API error ({code}): {scrub(str(msg))}")
        return data

    def get(self, path: str, **params: Any) -> dict:
        return self._call("GET", path, **params)

    def post(self, path: str, **params: Any) -> dict:
        return self._call("POST", path, **params)

    def get_all(self, path: str, **params: Any) -> list[dict]:
        """Follow paging.next until exhausted."""
        out: list[dict] = []
        data = self.get(path, **params)
        while True:
            out.extend(data.get("data", []))
            nxt = data.get("paging", {}).get("next")
            if not nxt:
                return out
            is_paused, why = paused()
            if is_paused:
                raise MetaRateLimited(why)
            try:
                resp = requests.get(nxt, headers={"Authorization": f"Bearer {self.token}"}, timeout=60)
                _read_usage(getattr(resp, "headers", None))
                data = resp.json()
            except (requests.RequestException, ValueError) as ex:
                raise MetaError(f"Network error while paging: {scrub(str(ex))}")
            if "error" in data:
                code = (data["error"] or {}).get("code")
                if code in THROTTLE_CODES:
                    _pause_for(30 * 60, "Meta asked the app to slow down (rate limit).")
                    raise MetaRateLimited("Meta asked the app to slow down; paging stopped for 30 minutes.")
                raise MetaError(scrub(data["error"].get("message", "paging error")))

    # ---------- account ----------

    def me(self) -> dict:
        return self.get("me", fields="id,name")

    def ad_accounts(self) -> list[dict]:
        return self.get_all("me/adaccounts", fields="id,account_id,name,currency,account_status", limit=100)

    def account(self, act: str) -> dict:
        return self.get(act, fields="id,name,currency,account_status,timezone_name")

    def pages(self) -> list[dict]:
        return self.get_all("me/accounts", fields="id,name", limit=100)

    def pixels(self, act: str) -> list[dict]:
        return self.get_all(f"{act}/adspixels", fields="id,name", limit=100)

    # ---------- insights ----------

    def insights_by_country(self, act: str, since: str, until: str) -> list[dict]:
        """Monthly rows per country. actions[] carries purchase counts."""
        return self.get_all(
            f"{act}/insights",
            level="account",
            breakdowns="country",
            time_increment="monthly",
            time_range={"since": since, "until": until},
            fields="spend,impressions,inline_link_clicks,actions,action_values,date_start,date_stop",
            limit=500,
        )

    def ad_insights(self, act: str, since: str, until: str) -> list[dict]:
        """One row per ad over the whole period: what each ad spent and sold."""
        return self.get_all(
            f"{act}/insights",
            level="ad",
            time_range={"since": since, "until": until},
            fields="ad_id,ad_name,adset_name,spend,impressions,inline_link_clicks,actions,action_values",
            limit=500,
        )

    # ---------- catalogue ----------

    def adsets_promoting(self, act: str) -> list[dict]:
        """Every ad set with what it promotes (a catalogue ad set carries promoted_object.product_set_id)."""
        return self.get_all(f"{act}/adsets", fields="id,name,promoted_object,campaign{id,name}", limit=200)

    def ads_product_sets(self, act: str) -> list[dict]:
        """Every ad with the product set of its creative (catalogue ads made in Ads Manager often keep the
        product set on the ad, not on the ad set)."""
        return self.get_all(f"{act}/ads", fields="adset_id,creative{product_set_id}", limit=500)

    def adset_insights(self, act: str, since: str, until: str) -> list[dict]:
        """One row per ad set over the whole period: what it spent and sold."""
        return self.get_all(f"{act}/insights", level="adset", time_range={"since": since, "until": until},
                            fields="adset_id,spend,impressions,inline_link_clicks,actions,action_values", limit=500)

    def product_set(self, product_set_id: str) -> dict:
        """Name, size and catalogue of a product set (needs the catalog_management permission)."""
        return self.get(product_set_id, fields="id,name,product_count,product_catalog{id,name}")

    def product_insights(self, act: str, since: str, until: str) -> list[dict]:
        """One row per catalogue product over the whole period ("<retailer id>, <name>" in product_id)."""
        return self.get_all(
            f"{act}/insights",
            level="account",
            breakdowns="product_id",
            time_range={"since": since, "until": until},
            fields="spend,impressions,inline_link_clicks,actions,action_values",
            limit=500,
        )

    def ad_copy(self, ad_id: str) -> dict:
        """The headline and text of an ad's creative (also for carousels)."""
        d = self.get(ad_id, fields="creative{title,body,object_story_spec}")
        cr = d.get("creative") or {}
        ld = ((cr.get("object_story_spec") or {}).get("link_data") or {})
        cards = [c.get("name", "") for c in ld.get("child_attachments", []) if c.get("name")]
        return {"title": cr.get("title") or ld.get("name") or (cards[0] if cards else ""),
                "body": cr.get("body") or ld.get("message") or "",
                "carousel": bool(ld.get("child_attachments"))}

    def campaign_insights(self, act: str, since: str, until: str) -> list[dict]:
        return self.get_all(
            f"{act}/insights",
            level="campaign",
            time_range={"since": since, "until": until},
            fields="campaign_id,campaign_name,objective,spend,impressions,inline_link_clicks,actions,action_values",
            limit=500,
        )

    # ---------- monitoring ----------

    def campaign_status(self, campaign_id: str) -> dict:
        return self.get(campaign_id, fields="id,name,status,effective_status,created_time")

    def campaign_objects(self, campaign_id: str) -> tuple[list[dict], list[dict]]:
        """(adsets, ads) with their effective statuses."""
        adsets = self.get_all(f"{campaign_id}/adsets", fields="id,name,effective_status,daily_budget", limit=200)
        ads = self.get_all(f"{campaign_id}/ads",
                           fields="id,name,adset_id,effective_status,issues_info", limit=500)
        return adsets, ads

    def campaign_insights_7d(self, campaign_id: str) -> dict[str, list[dict]]:
        """Last-7-day insights at campaign, adset and ad level."""
        fields = "spend,impressions,inline_link_clicks,actions,action_values,frequency,reach"
        out = {}
        for level in ("campaign", "adset", "ad"):
            extra = {"campaign": "", "adset": ",adset_id,adset_name", "ad": ",ad_id,ad_name,adset_id"}[level]
            out[level] = self.get_all(f"{campaign_id}/insights", level=level,
                                      date_preset="last_7d", fields=fields + extra, limit=500)
        return out

    def campaign_lifetime(self, campaign_id: str) -> dict:
        """Lifetime totals for the campaign (spend, purchases, revenue)."""
        rows = self.get_all(f"{campaign_id}/insights", level="campaign", date_preset="maximum",
                            fields="spend,impressions,inline_link_clicks,actions,action_values", limit=10)
        return rows[0] if rows else {}

    # ---------- updates (used by Apply / auto-actions) ----------

    def get_object(self, object_id: str, level: str = "adset") -> dict:
        # Ads have no daily_budget field; asking for it makes Graph reject the call.
        fields = "id,name,status,effective_status" + (",daily_budget" if level == "adset" else "")
        return self.get(object_id, fields=fields)

    def update_object(self, object_id: str, **fields: Any) -> dict:
        return self.post(object_id, **fields)

    def restore_now(self, object_id: str, **fields: Any) -> dict:
        """Put a value back after a half-done change, even while paused (one call, never repeated)."""
        return self._call("POST", object_id, _force=True, **fields)

    def delete_now(self, object_id: str) -> dict:
        """Remove something the app half-built, even while paused (a single call, never repeated)."""
        return self._call("POST", object_id, _force=True, status="DELETED")

    # ---------- images ----------

    def upload_image_bytes(self, act: str, data: bytes, name: str = "creative.png") -> str:
        """Upload an image to the ad account library; returns the image hash for link_data."""
        import base64
        res = self.post(f"{act}/adimages", bytes=base64.b64encode(data).decode(), name=name)
        images = res.get("images", {})
        if not images:
            raise MetaError("Image upload returned no hash.")
        return next(iter(images.values()))["hash"]

    # ---------- audiences ----------

    def create_custom_audience(self, act: str, name: str, rule: dict, description: str = "") -> str:
        return self.post(f"{act}/customaudiences", name=name, rule=rule, prefill="true",
                         description=description)["id"]

    def pixel_health(self, pixel_id: str, days: int = 7) -> dict:
        """Last fire time and event counts over the last `days` days."""
        import time as _t
        info = self.get(pixel_id, fields="name,last_fired_time,is_unavailable")
        counts: dict[str, int] = {}
        stats = self.get_all(f"{pixel_id}/stats", aggregation="event", start_time=int(_t.time()) - days * 86400)
        for bucket in stats:
            for item in bucket.get("data", []):
                counts[item.get("value", "")] = counts.get(item.get("value", ""), 0) + int(item.get("count", 0))
        return {"name": info.get("name", ""), "last_fired_time": info.get("last_fired_time"),
                "unavailable": bool(info.get("is_unavailable")), "counts": counts}

    # ---------- creation (always PAUSED) ----------

    def create_campaign(self, act: str, name: str, is_adset_budget_sharing_enabled: bool = False,
                        catalog_id: str = "") -> str:
        # Budgets live on ad sets (no CBO); Meta requires this flag to be explicit in that case.
        extra = {"promoted_object": {"product_catalog_id": catalog_id}} if catalog_id else {}
        return self.post(f"{act}/campaigns", name=name, objective="OUTCOME_SALES", status="PAUSED",
                         special_ad_categories=[],
                         is_adset_budget_sharing_enabled=str(is_adset_budget_sharing_enabled).lower(), **extra)["id"]

    def create_adset(self, act: str, **fields: Any) -> str:
        return self.post(f"{act}/adsets", status="PAUSED", **fields)["id"]

    def create_creative(self, act: str, **fields: Any) -> str:
        return self.post(f"{act}/adcreatives", **fields)["id"]

    def create_ad(self, act: str, name: str, adset_id: str, creative_id: str) -> str:
        return self.post(f"{act}/ads", name=name, adset_id=adset_id,
                         creative={"creative_id": creative_id}, status="PAUSED")["id"]


# Currencies Meta bills without minor units (budget sent as-is, not x100).
ZERO_DECIMAL = {"JPY", "KRW", "VND", "CLP", "ISK", "HUF", "TWD", "PYG", "UGX", "XAF", "XOF"}


def budget_minor_units(amount: float, currency: str) -> int:
    return int(round(amount)) if currency in ZERO_DECIMAL else int(round(amount * 100))
