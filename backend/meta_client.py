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

    def _call(self, method: str, path: str, **params: Any) -> dict:
        params = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
                  for k, v in params.items() if v is not None}
        # Token goes in a header, never in the URL, so it cannot leak into error text or logs.
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            if method == "GET":
                r = requests.get(f"{BASE}/{path}", params=params, headers=headers, timeout=60)
            else:
                r = requests.post(f"{BASE}/{path}", data=params, headers=headers, timeout=60)
        except requests.RequestException as ex:
            raise MetaError(f"Network error talking to Meta: {scrub(str(ex))}")
        try:
            data = r.json() if r.content else {}
        except ValueError:
            raise MetaError(f"Meta returned a non-JSON response ({r.status_code}): {scrub(r.text[:200])}")
        if r.status_code >= 400 or "error" in data:
            err = data.get("error", {})
            msg = err.get("error_user_msg") or err.get("message") or r.text
            raise MetaError(f"Meta API error ({err.get('code', r.status_code)}): {msg}")
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
            try:
                data = requests.get(nxt, headers={"Authorization": f"Bearer {self.token}"}, timeout=60).json()
            except (requests.RequestException, ValueError) as ex:
                raise MetaError(f"Network error while paging: {scrub(str(ex))}")
            if "error" in data:
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

    def create_campaign(self, act: str, name: str, is_adset_budget_sharing_enabled: bool = False) -> str:
        # Budgets live on ad sets (no CBO); Meta requires this flag to be explicit in that case.
        return self.post(f"{act}/campaigns", name=name, objective="OUTCOME_SALES", status="PAUSED",
                         special_ad_categories=[],
                         is_adset_budget_sharing_enabled=str(is_adset_budget_sharing_enabled).lower())["id"]

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
