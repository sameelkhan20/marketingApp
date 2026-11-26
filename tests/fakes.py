"""Fake Meta client used by monitor / action / launch tests. No network."""

from datetime import datetime, timedelta


def _row(**k):
    return {"spend": "0", "impressions": "0", "inline_link_clicks": "0", "frequency": "1", **k}


class FakeMeta:
    def __init__(self, token="x"):
        self.objects = {"A1": {"id": "A1", "status": "ACTIVE", "daily_budget": "1000"},
                        "A2": {"id": "A2", "status": "ACTIVE", "daily_budget": "1000"},
                        "AD1": {"id": "AD1", "status": "ACTIVE"}, "AD2": {"id": "AD2", "status": "ACTIVE"}}
        self.updates, self.ads_created, self.creatives_created, self.uploads = [], [], [], []
        self.campaign_effective_status = "ACTIVE"
        self.ads = [{"id": "AD1", "name": "Broad - C1", "adset_id": "A1", "effective_status": "ACTIVE"},
                    {"id": "AD2", "name": "Broad - C2", "adset_id": "A1", "effective_status": "ACTIVE"}]

    # account / monitoring
    def account(self, act):
        return {"id": act, "currency": "USD"}

    def campaign_status(self, cid):
        return {"id": cid, "name": "Fake camp", "effective_status": self.campaign_effective_status,
                "created_time": (datetime.utcnow() - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S+0000")}

    def campaign_objects(self, cid):
        return ([{"id": "A1", "name": "Broad", "effective_status": "ACTIVE"},
                 {"id": "A2", "name": "LAL", "effective_status": "ACTIVE"}], self.ads)

    def campaign_insights_7d(self, cid):
        return {
            "campaign": [_row(spend="200", impressions="20000", inline_link_clicks="300",
                              actions=[{"action_type": "omni_purchase", "value": "7"}],
                              action_values=[{"action_type": "omni_purchase", "value": "900"}])],
            "adset": [_row(adset_id="A1", adset_name="Broad", spend="160", impressions="15000", inline_link_clicks="100", frequency="3.4"),
                      _row(adset_id="A2", adset_name="LAL", spend="40", impressions="5000", inline_link_clicks="200",
                           actions=[{"action_type": "purchase", "value": "7"}],
                           action_values=[{"action_type": "purchase", "value": "900"}])],
            "ad": [_row(ad_id="AD1", ad_name="Broad - C1", adset_id="A1", spend="100", impressions="9000", inline_link_clicks="80",
                        actions=[{"action_type": "purchase", "value": "3"}], action_values=[{"action_type": "purchase", "value": "360"}]),
                   _row(ad_id="AD2", ad_name="Broad - C2", adset_id="A1", spend="80", impressions="6000", inline_link_clicks="20")],
        }

    def campaign_lifetime(self, cid):
        return {"spend": "300", "date_start": (datetime.utcnow() - timedelta(days=5)).date().isoformat(),
                "actions": [{"action_type": "omni_purchase", "value": "9"}],
                "action_values": [{"action_type": "omni_purchase", "value": "1080"}]}

    # updates
    def get_object(self, oid, level="adset"):
        return dict(self.objects[oid])

    def update_object(self, oid, **f):
        self.updates.append((oid, f))
        self.objects.setdefault(oid, {}).update({k: str(v) for k, v in f.items()})
        return {"success": True}

    # creation
    def create_campaign(self, act, name, **kw):
        return "CAMP1"

    def create_adset(self, act, **f):
        return f"AS{len(self.updates) + 1}"

    def create_creative(self, act, **f):
        self.creatives_created.append(f)
        return f"CR{len(self.creatives_created)}"

    def create_ad(self, act, name, adset_id, creative_id):
        i = f"NEW{len(self.ads_created) + 1}"
        self.ads_created.append({"id": i, "name": name, "adset_id": adset_id, "creative_id": creative_id})
        return i

    def upload_image_bytes(self, act, data, name="x"):
        self.uploads.append(name)
        return "HASH1"
