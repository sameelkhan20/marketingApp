"""Test setup: every test session gets its own data dir (SQLite + media), no network.

Set APP_DATA_DIR before the backend is imported so config/db pick it up.
"""

import os
import sys
import tempfile
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix="planner-test-")
os.environ["APP_DATA_DIR"] = _tmp
os.environ["ANTHROPIC_API_KEY"] = ""      # template plans, no Claude calls
os.environ["IMAGE_API_KEY"] = ""
os.environ["IMAGE_PROVIDER"] = "higgsfield"
os.environ["GMAIL_CLIENT_ID"] = ""
os.environ["GEMINI_API_KEY"] = ""
os.environ["ALERT_EMAIL"] = ""
os.environ["GMAIL_REFRESH_TOKEN"] = ""
os.environ["BREVO_API_KEY"] = ""
os.environ["META_APP_ID"] = ""
os.environ["PKR_PER_USD"] = "280"
os.environ["FX_LIVE"] = "0"  # no network in tests; test_fx switches it on with a fake feed
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from backend import monitor  # noqa: E402
from backend.db import Plan, PlanVersion, get_session, init_db  # noqa: E402
from backend.estimator import estimate_orders, estimate_to_dict  # noqa: E402
from backend.planner import _template_plan  # noqa: E402

PNG_1x1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d4944415478da63f8cfc0f01f0005000301ebf31f2b0000000049454e44ae426082"
)


@pytest.fixture(scope="session", autouse=True)
def _db():
    init_db()
    monitor.start_scheduler = lambda: None  # never start the timer in tests
    yield


@pytest.fixture
def plan():
    """A fresh approved plan (template content, US, leather jackets, 100k PKR / 30 days)."""
    inputs = {"budget_pkr": 100000, "country": "US", "product": "Black biker jacket", "niche": "leather_jackets",
              "price_usd": 120, "duration_days": 30}
    est = estimate_to_dict(estimate_orders(100000, "US", "leather_jackets", 120, 30))
    with get_session() as s:
        p = Plan(**inputs, estimate=est, status="approved")
        p.versions.append(PlanVersion(version=1, source="ai", content=_template_plan(inputs, est, [])))
        s.add(p)
        s.commit()
        s.refresh(p)
        return p.id


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from backend.main import app

    return TestClient(app)


OWNER = {"email": "owner@example.com", "password": "TestPass123!"}


@pytest.fixture
def auth(client):
    """Owner account (created the way the server command does) + bearer headers."""
    from backend.auth.routes import create_user
    try:
        create_user(OWNER["email"], OWNER["password"], superuser=True)
    except ValueError:
        pass  # already created by an earlier test
    tok = client.post("/api/auth/login", json=OWNER).json()
    return {"Authorization": f"Bearer {tok['access_token']}"}


@pytest.fixture(autouse=True)
def _fresh_login_throttle():
    """Failed sign-ins from earlier tests must not lock later ones out (all come from one test IP)."""
    from backend.auth import routes
    routes._fails.clear()
    yield
