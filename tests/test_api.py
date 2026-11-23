from backend import notify


def test_auth_flow(client):
    from tests.conftest import OWNER

    assert client.get("/api/plans").status_code == 401
    from backend.auth.routes import create_user
    try:
        create_user(OWNER["email"], OWNER["password"], superuser=True)
    except ValueError:
        pass
    # no public signup: registering without the owner's token is refused
    assert client.post("/api/users/register", json={"email": "x@example.com", "password": "TestPass123!"}).status_code == 403
    tok = client.post("/api/auth/login", json=OWNER)
    assert tok.status_code == 200
    h = {"Authorization": f"Bearer {tok.json()['access_token']}"}
    assert client.get("/api/users/me", headers=h).json()["email"] == OWNER["email"]
    assert client.get("/api/users/me", headers={"Authorization": f"Bearer {tok.json()['refresh_token']}"}).status_code == 401
    client.post("/api/auth/logout", headers=h)
    assert client.get("/api/users/me", headers=h).status_code == 401
    ref = client.post("/api/auth/refresh", json={"refresh_token": tok.json()["refresh_token"]})
    assert ref.status_code == 200
    assert client.get("/api/users/me", headers={"Authorization": f"Bearer {ref.json()['access_token']}"}).status_code == 200
    assert client.post("/api/auth/login", json={**OWNER, "password": "wrong"}).status_code == 401


def test_plan_lifecycle(client, auth):
    body = {"budget_pkr": 150000, "country": "GB", "product": "Aviator jacket", "niche": "leather_jackets",
            "price_usd": 140, "duration_days": 21}
    est = client.post("/api/estimate", json=body, headers=auth).json()
    assert est["orders_low"] <= est["orders_expected"] <= est["orders_high"]
    plan = client.post("/api/plans", json=body, headers=auth).json()
    assert plan["status"] == "draft" and plan["versions"][0]["content"]["creatives"][0]["visual_prompt"]
    pid = plan["id"]
    content = dict(plan["versions"][0]["content"])
    content["ad_sets"] = content["ad_sets"][:1]
    edited = client.post(f"/api/plans/{pid}/edit", json={"content": content, "note": "Broad only"}, headers=auth).json()
    assert len(edited["versions"]) == 2 and edited["learned_rules"] == ["Broad only"]
    assert client.post(f"/api/plans/{pid}/launch", headers=auth).status_code == 400   # not approved
    assert client.post(f"/api/plans/{pid}/approve", headers=auth).json()["status"] == "approved"
    ready = client.get(f"/api/plans/{pid}/readiness", headers=auth).json()
    assert ready["ready"] is False and any(c["key"] == "token" and not c["ok"] for c in ready["checks"])
    assert client.post(f"/api/plans/{pid}/launch", headers=auth).status_code == 400   # no Meta token
    assert any(r["rule"] == "Broad only" for r in client.get("/api/rules", headers=auth).json())


def test_meta_and_notify_status(client, auth):
    assert client.get("/api/meta/settings", headers=auth).json()["connected"] is False
    assert client.get("/api/meta/status", headers=auth).status_code == 400
    assert client.get("/api/notify/status", headers=auth).json()["configured"] is False
    assert client.post("/api/notify/test", headers=auth).status_code == 400
    assert client.get("/api/automation", headers=auth).json()["max_budget_pct"] == 20
    assert client.get("/api/creatives/status", headers=auth).json()["configured"] is False
    assert client.get("/api/outcomes", headers=auth).status_code == 200


def test_email_rendering_and_filter():
    from backend.db import Alert, Plan
    plan = Plan(id=999, product="Biker jacket", country="US")
    alerts = [Alert(id=1, plan_id=999, severity="critical", kind="no_purchases", object_level="adset", object_name="Broad",
                    message="$120 spent with 0 purchases", metrics={"spend_usd": 120},
                    suggestions=[{"diagnosis": "Not converting"}, {"action": "Pause", "expected_effect": "Stops waste", "risk": "none"}])]
    subject, html, text = notify.render_alert_email(alerts, {999: plan})
    assert "1 new alert (critical)" in subject and "Biker jacket" in html and "Not converting" in text
    subject, html, text = notify.render_digest_email(
        {"launches": [{"product": "Biker jacket", "country": "US", "spend_usd": 300, "purchases": 9, "predicted_so_far": 5,
                       "verdict": "ahead", "days_active": 10, "duration_days": 30, "open_alerts": 2}],
         "actions": [{"description": "Pause adset A1", "mode": "auto", "outcome": "improved"}], "alerts_open": 2})
    assert "Weekly" in subject and "Biker jacket" in html and "Pause adset A1" in text
