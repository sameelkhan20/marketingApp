import json
import secrets
from contextlib import asynccontextmanager
from urllib.parse import urlencode

import requests
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import func, select

from . import settings_store
from .benchmarks import COUNTRY_BENCHMARKS, DEFAULT_NICHE, NICHE_MULTIPLIERS
from .config import APP_BASE_URL, FRONTEND_DIR, MEDIA_DIR, META_API_VERSION, META_APP_ID, META_APP_SECRET
from . import fx
from . import actions, activity, creatives, gmail_auth, higgsfield, imagegen, monitor, notify, outcomes, readiness, usage
from .auth.dependencies import user_from_token
from .auth.routes import auth_router, users_router
from .db import ActionLog, ActionOutcome, Alert, AccountStat, Calibration, Creative, Launch, LearnedRule, PlanOutcome, MetricSnapshot, Plan, PlanVersion, get_session, init_db
from .estimator import estimate_orders, estimate_to_dict
from .history import account_overrides
from .launcher import launch_plan
from .learning import active_rules, learn_from_edit, reinforce_on_approval
from .meta_client import MetaClient, MetaError
from .meta_sync import sync_account
from .planner import generate_plan

@asynccontextmanager
async def _lifespan(_app):
    monitor.start_scheduler()
    from . import backup
    backup.start_scheduler()
    yield


init_db()

# Owner-only routes, by their route template: checked after FastAPI has matched the route, so no
# spelling of the URL ("+1", "1.0", "%201") can slip past it.
OWNER_ROUTES = {
    ("POST", "/api/meta/settings"), ("GET", "/api/meta/oauth/start"), ("POST", "/api/automation"),
    ("POST", "/api/billing"), ("POST", "/api/meta/sync"),
    ("POST", "/api/higgsfield/connect"), ("POST", "/api/higgsfield/complete"), ("POST", "/api/higgsfield/disconnect"),
    ("POST", "/api/gmail/connect"), ("POST", "/api/gmail/complete"), ("POST", "/api/gmail/disconnect"),
    # anything that creates or changes spend on Meta, or throws results away
    ("POST", "/api/plans/{plan_id}/launch"), ("POST", "/api/alerts/{alert_id}/apply"),
    ("POST", "/api/actions/{log_id}/undo"), ("POST", "/api/launches/{launch_id}/complete"),
    ("DELETE", "/api/plans/{plan_id}"),
    ("GET", "/api/backups"), ("POST", "/api/backups"), ("GET", "/api/backups/{name}"),
    ("GET", "/api/brands/{brand_id}"), ("POST", "/api/brands"), ("POST", "/api/brands/{brand_id}"),
    ("DELETE", "/api/brands/{brand_id}"), ("POST", "/api/brands/{brand_id}/check"),
    ("POST", "/api/brands/{brand_id}/winners"),
}


def _owner_guard(request: Request) -> None:
    route = request.scope.get("route")
    if route is None or (request.method, getattr(route, "path", "")) not in OWNER_ROUTES:
        return
    user = getattr(request.state, "user", None) or {}
    if not user.get("is_superuser"):
        raise HTTPException(403, "Only the owner can change this")


app = FastAPI(title="Meta Ads Planner", lifespan=_lifespan, docs_url=None, redoc_url=None, openapi_url=None,
              dependencies=[Depends(_owner_guard)])


# ---------- auth (ported from core_auth_fixed) ----------

app.include_router(auth_router)
app.include_router(users_router)

# Every /api/* route needs a valid access token except these.
PUBLIC_API = {
    "/api/auth/login", "/api/auth/refresh", "/api/auth/logout", "/api/auth/setup-status",
    "/api/users/register", "/api/meta/oauth/callback",
}
MAX_UPLOAD_BYTES = 15 * 1024 * 1024


MAX_JSON_BYTES = 1024 * 1024


@app.middleware("http")
async def require_auth(request: Request, call_next):
    import re

    from starlette.concurrency import run_in_threadpool
    path = request.url.path
    # refuse oversized bodies before reading them (uploads have their own 15 MB limit)
    size = request.headers.get("content-length", "")
    limit = MAX_UPLOAD_BYTES + 64 * 1024 if "multipart/" in request.headers.get("content-type", "") else MAX_JSON_BYTES
    if size.isdigit() and int(size) > limit:
        return JSONResponse({"detail": "Request too large"}, status_code=413)
    if path.startswith("/api/") and path not in PUBLIC_API and request.method != "OPTIONS":
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            return JSONResponse({"detail": "Not authenticated"}, status_code=401,
                                headers={"WWW-Authenticate": "Bearer"})
        try:
            # SQLite work off the event loop, so a busy database never freezes every request
            request.state.user = await run_in_threadpool(user_from_token, header[7:])
        except HTTPException as ex:
            return JSONResponse({"detail": ex.detail}, status_code=ex.status_code, headers=ex.headers)
    return await call_next(request)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    h = response.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("Referrer-Policy", "same-origin")
    if not request.url.path.startswith("/api/"):
        h.setdefault("Content-Security-Policy",
                     "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                     "font-src https://fonts.gstatic.com; img-src 'self' data: blob:; connect-src 'self'; "
                     "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
    if request.url.scheme == "https":
        h.setdefault("Strict-Transport-Security", "max-age=31536000")
    return response


# ---------- activity log for every change made through the app ----------

# (method, path regex) -> (kind, action, title template). {0},{1} are the path ids.
ACTIVITY_ROUTES = [
    ("POST", r"/api/plans", "plan", "created", "Plan generated"),
    ("POST", r"/api/plans/(\d+)/edit", "plan", "edited", "Plan #{0} edited"),
    ("POST", r"/api/plans/(\d+)/approve", "plan", "approved", "Plan #{0} approved"),
    ("DELETE", r"/api/plans/(\d+)", "plan", "deleted", "Plan #{0} deleted"),
    ("POST", r"/api/backups", "settings", "backup", "Database backup made"),
    ("POST", r"/api/plans/(\d+)/launch", "launch", "launched", "Plan #{0} launched on Meta, paused"),
    ("POST", r"/api/launches/(\d+)/complete", "launch", "closed", "Launch #{0} closed and learned from"),
    ("POST", r"/api/plans/(\d+)/creatives/generate", "image", "generated", "Ad image generated for plan #{0}"),
    ("POST", r"/api/plans/(\d+)/creatives/upload", "image", "uploaded", "Ad image uploaded for plan #{0}"),
    ("DELETE", r"/api/creatives/(\d+)", "image", "deleted", "Ad image #{0} deleted"),
    ("POST", r"/api/creatives/product-photo", "image", "product_photo", "Product photo replaced"),
    ("POST", r"/api/creatives/brand-style", "settings", "brand_style", "Brand style saved"),
    ("POST", r"/api/rules", "learning", "rule_added", "Rule added by hand"),
    ("DELETE", r"/api/rules/(\d+)", "learning", "rule_deactivated", "Rule #{0} deactivated"),
    ("POST", r"/api/meta/settings", "settings", "meta_saved", "Meta settings saved"),
    ("POST", r"/api/meta/sync", "settings", "meta_synced", "Meta history synced"),
    ("POST", r"/api/brands/(\d+)/winners", "settings", "winners_synced", "Best ads synced for store #{0}"),
    ("POST", r"/api/brands", "settings", "store_added", "Store added"),
    ("POST", r"/api/brands/(\d+)", "settings", "store_saved", "Store #{0} saved"),
    ("DELETE", r"/api/brands/(\d+)", "settings", "store_deleted", "Store #{0} deleted"),
    ("POST", r"/api/monitor/run", "monitor", "manual_check", "Campaigns checked by hand"),
    ("POST", r"/api/alerts/(\d+)/apply", "action", "applied", "Suggestion applied on Meta for alert #{0}"),
    ("POST", r"/api/alerts/(\d+)/(acknowledge|resolve|reopen)", "monitor", "alert", "Alert #{0}: {1}"),
    ("POST", r"/api/actions/(\d+)/undo", "action", "undone", "Change #{0} undone on Meta"),
    ("POST", r"/api/automation", "settings", "automation", "Automation settings saved"),
    ("POST", r"/api/billing", "settings", "billing", "Budget and prices saved"),
    ("POST", r"/api/notify/test", "email", "test", "Test email sent"),
    ("POST", r"/api/notify/digest", "email", "digest", "Weekly digest sent by hand"),
    ("POST", r"/api/(higgsfield|gmail)/complete", "settings", "connected", "{0} connected"),
    ("POST", r"/api/(higgsfield|gmail)/disconnect", "settings", "disconnected", "{0} disconnected"),
]


@app.middleware("http")
async def record_activity(request: Request, call_next):
    import re

    from starlette.responses import Response

    response = await call_next(request)
    if request.method not in ("POST", "DELETE") or not request.url.path.startswith("/api/"):
        return response
    match = None
    for method, pattern, kind, action, title in ACTIVITY_ROUTES:
        m = re.fullmatch(pattern, request.url.path)
        if m and method == request.method:
            match = (m, kind, action, title)
            break
    if not match or response.status_code == 401:
        return response
    m, kind, action, title = match
    body = b"".join([chunk async for chunk in response.body_iterator])
    detail, level = "", "ok"
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        data = {}
    if response.status_code >= 400:
        level = "error" if response.status_code >= 500 else "warning"
        detail = str(data.get("detail", "")) if isinstance(data, dict) else ""
        title = title + " failed"
    elif isinstance(data, dict):
        # a short, useful line from the answer
        if action == "created":
            title = f"Plan #{data.get('id')} generated: {data.get('inputs', {}).get('product', '')} to {data.get('inputs', {}).get('country', '')}"
        elif action in ("applied", "undone"):
            detail = data.get("description", "")
        elif action == "manual_check":
            detail = f"{len(data.get('checked', []))} campaigns checked" + (f"; errors: {'; '.join(data['errors'])}" if data.get("errors") else "")
            level = "warning" if data.get("errors") else "ok"
        elif action == "meta_synced":
            detail = f"{data.get('rows', 0)} rows, {data.get('countries', 0)} countries, {data.get('since', '')} to {data.get('until', '')}"
        elif action == "launched":
            notes = (data.get("launches") or [{}])[-1].get("notes", [])
            detail = "; ".join(notes)
        elif action == "connected":
            detail = data.get("email", "")
        elif action == "rule_added":
            detail = data.get("rule", "")
        elif action == "edited":
            detail = ", ".join(data.get("learned_rules") or []) and f"Learned: {'; '.join(data['learned_rules'])}"
        elif action == "generated":
            detail = f"Creative {data.get('creative_index', 0) + 1}, {data.get('format', '')}"
        elif action == "test":
            detail = f"to {data.get('to', '')} via {data.get('via', '')}"
    groups = [g.capitalize() if g in ("higgsfield", "gmail") else g for g in m.groups()]
    from starlette.concurrency import run_in_threadpool
    await run_in_threadpool(activity.record, kind, action, title.format(*groups), detail, level,
                            (getattr(request.state, "user", None) or {}).get("email", ""))
    return Response(content=body, status_code=response.status_code, headers=dict(response.headers),
                    media_type=response.media_type)


# ---------- schemas ----------

class PlanRequest(BaseModel):
    budget_pkr: float = Field(gt=0)
    country: str = Field(min_length=2, max_length=2)
    product: str = Field(min_length=2, max_length=200)
    niche: str = DEFAULT_NICHE

    @field_validator("niche")
    @classmethod
    def _known_niche(cls, v: str) -> str:
        v = (v or DEFAULT_NICHE).strip().lower()
        if v not in NICHE_MULTIPLIERS:
            raise ValueError(f"niche must be one of {', '.join(NICHE_MULTIPLIERS)}")
        return v

    @model_validator(mode="after")
    def _other_needs_words(self):
        if self.niche == "other" and len(self.category_note.strip()) < 3:
            raise ValueError("For 'Other', say what the product is (for example: prayer mats, phone cases)")
        return self
    price_usd: float = Field(gt=0, default=120)
    duration_days: int = Field(ge=3, le=90, default=30)
    category_note: str = Field(default="", max_length=200)  # "what is it", most useful for "other"
    brand_id: int = Field(default=0, ge=0)                    # which store (backend/brands.py)
    # the sales brief: facts the plan may use (it never invents them)
    offer: str = Field(default="", max_length=200)
    usp: str = Field(default="", max_length=300)
    delivery_days: int = Field(default=0, ge=0, le=90)
    returns: str = Field(default="", max_length=150)
    social_proof: str = Field(default="", max_length=200)
    cod: bool = False                                          # cash on delivery offered
    return_rate_pct: float = Field(default=0, ge=0, le=80)     # orders refused or returned
    unit_cost_usd: float = Field(ge=0, default=0)   # what one item costs you
    shipping_usd: float = Field(ge=0, default=0)    # shipping + packing per order


class EditRequest(BaseModel):
    content: dict
    note: str = Field(default="", max_length=2000)


class RuleRequest(BaseModel):
    rule: str = Field(min_length=3, max_length=500)


# ---------- helpers ----------

def _policy(p: Plan) -> list[dict]:
    from .adpolicy import check_plan
    try:
        return check_plan(p.versions[-1].content if p.versions else {}, p.brief or {}, p.niche, p.product)
    except Exception:  # a check must never stop a plan from opening
        return []


def _plan_out(p: Plan) -> dict:
    launches = [
        {"id": l.id, "campaign_id": l.campaign_id, "status": l.status,
         "created_at": l.created_at.isoformat(), **l.objects}
        for l in p.launches
    ]
    return {
        "id": p.id,
        "status": p.status,
        "created_at": p.created_at.isoformat(),
        "launches": launches,
        "policy": _policy(p),
        "inputs": {
            "budget_pkr": p.budget_pkr, "country": p.country, "product": p.product,
            "niche": p.niche, "price_usd": p.price_usd, "duration_days": p.duration_days,
            "category_note": p.category_note or "",
            "brand_id": p.brand_id or 0,
            **{k: (p.brief or {}).get(k, 0 if k == "delivery_days" else "")
               for k in ("offer", "usp", "delivery_days", "returns", "social_proof")},
            "cod": bool((p.brief or {}).get("cod", False)),
            "return_rate_pct": (p.brief or {}).get("return_rate_pct", 0),
            "unit_cost_usd": (p.estimate or {}).get("assumptions", {}).get("unit_cost_usd", 0),
            "shipping_usd": (p.estimate or {}).get("assumptions", {}).get("shipping_usd", 0),
        },
        "estimate": p.estimate,
        "versions": [
            {"version": v.version, "source": v.source, "note": v.note,
             "created_at": v.created_at.isoformat(), "content": v.content}
            for v in p.versions
        ],
    }


# ---------- routes ----------

@app.get("/api/meta")
def meta():
    return {
        "countries": {k: v["name"] for k, v in COUNTRY_BENCHMARKS.items()},
        "niches": {k: v["label"] for k, v in NICHE_MULTIPLIERS.items()},
        "categories": [{"key": k, "label": v["label"], "group": v["group"], "typical_price": v["typical_price"]}
                       for k, v in NICHE_MULTIPLIERS.items()],
        "default_niche": DEFAULT_NICHE,
        "pkr_per_usd": fx.pkr_per_usd(), "fx": fx.info(),
    }


def _estimate(req: PlanRequest) -> dict:
    with get_session() as s:
        cal = outcomes.calibration_factor(s, req.country, req.brand_id, req.niche)
    overrides = account_overrides(req.country, req.niche, req.price_usd, req.brand_id)
    if overrides:
        cal = (1.0, 0)  # the store's own history is already real results: no second correction
    try:
        e = estimate_orders(
            req.budget_pkr, req.country, req.niche, req.price_usd, req.duration_days,
            overrides=overrides, calibration=cal,
            unit_cost_usd=req.unit_cost_usd, shipping_usd=req.shipping_usd, delivery_days=req.delivery_days,
            cod=req.cod, return_rate_pct=req.return_rate_pct,
        )
    except KeyError as ex:
        raise HTTPException(400, str(ex))
    return estimate_to_dict(e)


@app.post("/api/estimate")
def estimate(req: PlanRequest):
    return _estimate(req)


@app.post("/api/plans")
def create_plan(req: PlanRequest):
    e = _estimate(req)

    from . import brands
    with get_session() as s:
        if not brands.exists(s, req.brand_id):
            raise HTTPException(400, "That brand does not exist")
        from .learning import active_rule_rows
        rule_rows = active_rule_rows(s, req.country, req.niche, limit=15, brand_id=req.brand_id)
        rules = [r.rule for r in rule_rows]
        try:
            has_pixel = bool(brands.settings_for(s, req.brand_id)["meta_pixel_id"])
            from .meta_sync import winners_for
            content = generate_plan({**req.model_dump(), "has_pixel": has_pixel}, e, rules, winners_for(s, req.brand_id))
        except Exception as ex:  # surfaces API/key problems to the UI
            raise HTTPException(502, f"Plan generation failed: {ex}")

        # remember which rules shaped an AI plan, so approval and results credit only those
        e = {**e, "rules_used": [] if str(content.get("summary", "")).startswith("[TEMPLATE") else [r.id for r in rule_rows]}
        p = Plan(
            budget_pkr=req.budget_pkr, country=req.country.upper(), product=req.product,
            niche=req.niche, price_usd=req.price_usd, duration_days=req.duration_days, estimate=e,
            category_note=req.category_note.strip(), brand_id=req.brand_id,
            brief={k: (v.strip() if isinstance(v, str) else v) for k, v in req.model_dump().items()
                   if k in ("offer", "usp", "delivery_days", "returns", "social_proof", "cod", "return_rate_pct")},
        )
        p.versions.append(PlanVersion(version=1, source="ai", content=content))
        s.add(p)
        s.commit()
        s.refresh(p)
        return _plan_out(p)


@app.get("/api/plans")
def list_plans():
    with get_session() as s:
        plans = s.scalars(select(Plan).order_by(Plan.id.desc())).all()
        po_by_plan = {po.plan_id: po for po in s.scalars(select(PlanOutcome).order_by(PlanOutcome.updated_at))}
        return [
            {"id": p.id, "status": p.status, "product": p.product, "country": p.country,
             "budget_pkr": p.budget_pkr, "orders": p.estimate["orders_expected"],
             "created_at": p.created_at.isoformat(),
             "outcome": _po_out(po_by_plan[p.id]) if p.id in po_by_plan else None}
            for p in plans
        ]


def _po_out(po: PlanOutcome) -> dict:
    return {"launch_id": po.launch_id, "plan_id": po.plan_id, "days_active": po.days_active, "duration_days": po.duration_days,
            "spend_usd": po.spend_usd, "purchases": po.purchases, "revenue_usd": po.revenue_usd,
            "predicted_expected": po.predicted_expected, "predicted_low": po.predicted_low,
            "predicted_high": po.predicted_high, "predicted_so_far": po.predicted_so_far, "ratio": po.ratio,
            "verdict": po.verdict, "completed": bool(po.completed), "summary": po.summary,
            "rules_added": po.rules_added, "updated_at": po.updated_at.isoformat()}


@app.get("/api/plans/{plan_id}")
def get_plan(plan_id: int):
    with get_session() as s:
        p = s.get(Plan, plan_id)
        if not p:
            raise HTTPException(404, "Plan not found")
        return _plan_out(p)




def _validate_plan_content(c: dict, duration_days: int | None = None) -> dict:
    """Edited plans go through the same cleanup as AI plans: known fields only, real numbers."""
    from .planner import PlanError, normalize_plan
    try:
        return normalize_plan(c, duration_days)
    except PlanError as ex:
        raise HTTPException(422, f"Invalid plan: {ex}")


@app.post("/api/plans/{plan_id}/edit")
def edit_plan(plan_id: int, req: EditRequest):
    with get_session() as s:
        p = s.get(Plan, plan_id)
        if not p:
            raise HTTPException(404, "Plan not found")
        if p.status in ("launched", "launching"):
            raise HTTPException(400, "Launched plans cannot be edited" if p.status == "launched"
                                else "This plan is being launched right now; wait a moment")
        content = _validate_plan_content(req.content, p.duration_days)
        previous = p.versions[-1].content  # learn only what changed in this edit
        try:  # compare like with like: an older version may predate fields the cleanup now adds
            from .planner import PlanError, normalize_plan
            previous = normalize_plan(json.loads(json.dumps(previous)), p.duration_days)
        except PlanError:
            pass
        p.versions.append(PlanVersion(version=len(p.versions) + 1, source="user",
                                      content=content, note=req.note))
        p.status = "draft"  # edited content must be approved again before it can launch
        s.commit()
        try:
            learned = learn_from_edit(s, p.id, previous, content, req.note)
        except Exception as ex:  # the edit is saved; learning is best-effort
            s.rollback()
            learned = []
            print(f"learning failed for plan {p.id}: {ex}")
        s.refresh(p)
        out = _plan_out(p)
        out["learned_rules"] = learned
        return out


@app.post("/api/plans/{plan_id}/approve")
def approve_plan(plan_id: int):
    with get_session() as s:
        p = s.get(Plan, plan_id)
        if not p:
            raise HTTPException(404, "Plan not found")
        if p.status != "draft":
            raise HTTPException(400, f"Only draft plans can be approved (this one is {p.status}).")
        p.status = "approved"
        s.commit()
        if len(p.versions) == 1:
            reinforce_on_approval(s, p.id)
        s.refresh(p)
        return _plan_out(p)


@app.delete("/api/plans/{plan_id}")
def delete_plan(plan_id: int):
    """Drafts and approved plans that never reached Meta. Launched plans stay: their results teach the app."""
    with get_session() as s:
        p = s.get(Plan, plan_id)
        if not p:
            raise HTTPException(404, "Plan not found")
        if p.status == "launching":
            raise HTTPException(400, "This plan is being launched right now; wait a moment")
        if p.launches or p.status == "launched":
            raise HTTPException(400, "This plan was launched on Meta, so it is kept for its results.")
        for c in s.scalars(select(Creative).where(Creative.plan_id == plan_id)):
            creatives.delete_file(c.file_name)
            s.delete(c)
        s.delete(p)
        s.commit()
        return {"ok": True}


# ---------- backups (owner only) ----------

def _owner(request: Request) -> None:
    if not request.state.user.get("is_superuser"):
        raise HTTPException(403, "Only the owner can manage backups")


@app.get("/api/backups")
def list_backups(request: Request):
    from . import backup
    _owner(request)
    return backup.list_all()


@app.post("/api/backups")
def make_backup(request: Request):
    from . import backup
    _owner(request)
    return backup.run()


@app.get("/api/backups/{name}")
def download_backup(name: str, request: Request):
    from . import backup
    _owner(request)
    path = backup.path_of(name)
    if not path:
        raise HTTPException(404, "Backup not found")
    return FileResponse(path, media_type="application/octet-stream", filename=name)


# ---------- brands: one Meta connection per store ----------

class BrandRequest(BaseModel):
    values: dict[str, str]


@app.get("/api/brands")
def list_brands():
    from . import brands
    with get_session() as s:
        return brands.all_brands(s)


@app.get("/api/brands/{brand_id}")
def get_brand(brand_id: int):
    from . import brands
    with get_session() as s:
        if not brands.exists(s, brand_id):
            raise HTTPException(404, "Brand not found")
        return brands.brand_out(s, brand_id)


@app.post("/api/brands")
def create_brand(req: BrandRequest):
    from . import brands
    with get_session() as s:
        try:
            bid = brands.save(s, None, req.values)
        except brands.BrandError as ex:
            raise HTTPException(400, str(ex))
        return brands.brand_out(s, bid)


@app.post("/api/brands/{brand_id}")
def update_brand(brand_id: int, req: BrandRequest):
    from . import brands
    with get_session() as s:
        try:
            brands.save(s, brand_id, req.values)
        except brands.BrandError as ex:
            raise HTTPException(400, str(ex))
        return brands.brand_out(s, brand_id)


@app.delete("/api/brands/{brand_id}")
def delete_brand(brand_id: int):
    from . import brands
    with get_session() as s:
        try:
            brands.delete(s, brand_id)
        except brands.BrandError as ex:
            raise HTTPException(400, str(ex))
        return {"ok": True}


@app.post("/api/brands/{brand_id}/winners")
def sync_brand_winners(brand_id: int):
    """Read this store's ad history and keep its best ads for the planner."""
    from . import brands
    from .meta_sync import sync_winners
    with get_session() as s:
        try:
            return sync_winners(s, brand_id)
        except brands.BrandError as ex:
            raise HTTPException(404, str(ex))
        except MetaError as ex:
            raise HTTPException(400, str(ex))


@app.get("/api/brands/{brand_id}/winners")
def list_brand_winners(brand_id: int):
    from .meta_sync import winners_for
    with get_session() as s:
        return winners_for(s, brand_id)


@app.post("/api/brands/{brand_id}/check")
def check_brand(brand_id: int):
    """What this brand's token can see: the user, ad accounts, pages and pixels."""
    from . import brands
    with get_session() as s:
        try:
            vals = brands.settings_for(s, brand_id)
        except brands.BrandError as ex:
            raise HTTPException(404, str(ex))
    try:
        c = MetaClient(vals["meta_access_token"])
        out = {"user": c.me(), "accounts": c.ad_accounts(), "pages": c.pages(), "pixels": []}
        if vals["meta_ad_account_id"]:
            out["pixels"] = c.pixels(settings_store.ad_account_path(vals["meta_ad_account_id"]))
        return out
    except MetaError as ex:
        raise HTTPException(400, str(ex))


@app.get("/api/brief")
def last_brief(brand_id: int = 0):
    """The sales brief of this store's latest plan, to prefill the next one."""
    with get_session() as s:
        p = s.scalar(select(Plan).where(Plan.brand_id == brand_id).order_by(Plan.id.desc()).limit(1))
        return (p.brief or {}) if p else {}


@app.get("/api/rules")
def list_rules():
    with get_session() as s:
        rows = s.scalars(select(LearnedRule).order_by(LearnedRule.id.desc())).all()
        return [{"id": r.id, "rule": r.rule, "weight": r.weight, "active": bool(r.active),
                 "source_plan_id": r.source_plan_id, "created_at": r.created_at.isoformat()}
                for r in rows]


@app.post("/api/rules")
def add_rule(req: RuleRequest):
    with get_session() as s:
        r = LearnedRule(rule=req.rule.strip())
        s.add(r)
        s.commit()
        return {"id": r.id, "rule": r.rule}


@app.delete("/api/rules/{rule_id}")
def deactivate_rule(rule_id: int):
    with get_session() as s:
        r = s.get(LearnedRule, rule_id)
        if not r:
            raise HTTPException(404, "Rule not found")
        r.active = 0
        s.commit()
        return {"ok": True}


# ---------- Meta: settings + connect ----------

class SettingsRequest(BaseModel):
    values: dict[str, str]


@app.get("/api/meta/settings")
def meta_settings():
    with get_session() as s:
        vals = settings_store.get_all(s)
    return {
        "settings": settings_store.masked(vals),
        "connected": bool(vals["meta_access_token"] and vals["meta_ad_account_id"]),
        "oauth_available": META_OAUTH_ENABLED,
    }


@app.post("/api/meta/settings")
def save_meta_settings(req: SettingsRequest):
    with get_session() as s:
        settings_store.set_many(s, {k: v for k, v in req.values.items() if k in settings_store.FORM_KEYS})
        return {"settings": settings_store.masked(settings_store.get_all(s))}


@app.get("/api/meta/status")
def meta_status():
    """Validates the token and lists what the token can see (accounts, pages, pixels)."""
    with get_session() as s:
        vals = settings_store.get_all(s)
    try:
        c = MetaClient(vals["meta_access_token"])
        me = c.me()
        accounts = c.ad_accounts()
        pages = c.pages()
        pixels = []
        if vals["meta_ad_account_id"]:
            try:
                pixels = c.pixels(settings_store.ad_account_path(vals["meta_ad_account_id"]))
            except MetaError as ex:
                pixels = [{"id": "", "name": f"(could not list pixels: {ex})"}]
    except MetaError as ex:
        raise HTTPException(400, str(ex))
    return {"user": me, "accounts": accounts, "pages": pages, "pixels": pixels}


@app.post("/api/meta/sync")
def meta_sync():
    with get_session() as s:
        vals = settings_store.get_all(s)
        try:
            result = sync_account(s, vals["meta_access_token"], vals["meta_ad_account_id"])
        except MetaError as ex:
            raise HTTPException(400, str(ex))
        from .meta_sync import sync_winners
        try:
            result["winners"] = sync_winners(s, 0)["winners"]
        except Exception as ex:  # the history itself synced; the best-ads list can come next time
            result["winners_error"] = str(ex)
    return result


@app.get("/api/meta/stats")
def meta_stats():
    with get_session() as s:
        rows = s.scalars(select(AccountStat).order_by(AccountStat.spend_usd.desc())).all()
        return [
            {"country": r.country, "name": COUNTRY_BENCHMARKS.get(r.country, {}).get("name", r.country),
             "spend_usd": r.spend_usd, "impressions": r.impressions, "clicks": r.clicks,
             "purchases": r.purchases, "revenue_usd": r.revenue_usd, "cpm_usd": r.cpm_usd,
             "ctr": r.ctr, "cvr": r.cvr, "confidence": r.confidence,
             "since": r.since, "until": r.until, "synced_at": r.synced_at.isoformat()}
            for r in rows
        ]


# OAuth (optional): needs META_APP_ID / META_APP_SECRET in .env and the
# redirect URI below whitelisted in the Meta app's Facebook Login settings.
# Facebook login (a ~60-day user token for the main store only) is off: stores use System User
# tokens, which never expire. META_OAUTH=1 in .env turns it back on.
META_OAUTH_ENABLED = __import__("os").getenv("META_OAUTH", "0") == "1"
_oauth_states: dict[str, float] = {}  # state -> created (unix seconds); valid 15 minutes
OAUTH_REDIRECT = f"{APP_BASE_URL}/api/meta/oauth/callback"
OAUTH_SCOPES = "ads_management,ads_read,business_management,pages_show_list,pages_read_engagement"


@app.get("/api/meta/oauth/start")
def oauth_start():
    """Returns the Facebook login URL. Requires the app login, so only the owner can mint a valid
    state; the public callback then only accepts that state (prevents token substitution)."""
    if not META_OAUTH_ENABLED:
        raise HTTPException(404, "Facebook login is turned off: paste a System User token on the Connections page.")
    if not (META_APP_ID and META_APP_SECRET):
        raise HTTPException(400, "META_APP_ID / META_APP_SECRET not set in .env; paste a token instead.")
    import time
    now = time.time()
    for old in [k for k, t in _oauth_states.items() if now - t > 900]:
        _oauth_states.pop(old, None)
    state = secrets.token_urlsafe(16)
    _oauth_states[state] = now
    url = f"https://www.facebook.com/{META_API_VERSION}/dialog/oauth?" + urlencode({
        "client_id": META_APP_ID, "redirect_uri": OAUTH_REDIRECT, "state": state, "scope": OAUTH_SCOPES,
    })
    return {"url": url}


@app.get("/api/meta/oauth/callback")
def oauth_callback(code: str = "", state: str = "", error_description: str = ""):
    if error_description:
        raise HTTPException(400, error_description)
    import time
    created = _oauth_states.pop(state, None)
    if created is None or time.time() - created > 900:
        raise HTTPException(400, "This Facebook login link expired. Start again from Connections.")
    if not code:
        raise HTTPException(400, "Facebook did not return a code")
    base = f"https://graph.facebook.com/{META_API_VERSION}/oauth/access_token"
    try:  # never let a requests error (its text holds the URL with the app secret) reach the logs
        short = requests.get(base, params={"client_id": META_APP_ID, "client_secret": META_APP_SECRET,
                                           "redirect_uri": OAUTH_REDIRECT, "code": code}, timeout=30).json()
        if "access_token" not in short:
            msg = (short.get("error") or {}).get("message", "unknown error") if isinstance(short, dict) else "unknown error"
            raise HTTPException(400, f"Facebook refused the login: {msg}")
        long = requests.get(base, params={"grant_type": "fb_exchange_token", "client_id": META_APP_ID,
                                          "client_secret": META_APP_SECRET,
                                          "fb_exchange_token": short["access_token"]}, timeout=30).json()
    except (requests.RequestException, ValueError):
        raise HTTPException(502, "Could not reach Facebook. Try again.") from None
    token = long.get("access_token", short["access_token"])
    with get_session() as s:
        settings_store.set_many(s, {"meta_access_token": token})
    return RedirectResponse("/#meta")


# ---------- Meta: launch ----------

@app.get("/api/plans/{plan_id}/readiness")
def plan_readiness(plan_id: int, live: bool = True):
    with get_session() as s:
        p = s.get(Plan, plan_id)
        if not p:
            raise HTTPException(404, "Plan not found")
        return readiness.check_plan(s, p, live=live)


@app.post("/api/plans/{plan_id}/launch")
def launch(plan_id: int):
    with get_session() as s:
        p = s.get(Plan, plan_id)
        if not p:
            raise HTTPException(404, "Plan not found")
        if p.status != "approved":
            raise HTTPException(400, "Approve the plan before launching." if p.status == "draft"
                                else f"This plan is already {p.status}.")
        ready = readiness.check_plan(s, p, live=False)
        if not ready["ready"]:
            raise HTTPException(400, "Not ready: " + "; ".join(c["text"] for c in ready["checks"] if c["hard"] and not c["ok"]))
        # Claim the plan in one statement: a double click (two requests) can never build two campaigns.
        from sqlalchemy import update
        claimed = s.execute(update(Plan).where(Plan.id == plan_id, Plan.status == "approved")
                            .values(status="launching")).rowcount
        s.commit()
        if claimed != 1:
            raise HTTPException(409, "This plan is already being launched.")
        s.refresh(p)
        from . import brands
        try:
            vals = brands.settings_for(s, p.brand_id)
        except brands.BrandError as ex:
            p.status = "approved"
            s.commit()
            raise HTTPException(400, str(ex))
        inputs = {"budget_pkr": p.budget_pkr, "country": p.country, "product": p.product,
                  "niche": p.niche, "price_usd": p.price_usd, "duration_days": p.duration_days}
        content = json.loads(json.dumps(p.versions[-1].content))  # exactly the approved version
        try:
            # Upload the plan's selected creatives to Meta first (once each), then launch with their hashes.
            selected = s.scalars(select(Creative).where(Creative.plan_id == p.id, Creative.selected == 1)
                                 .order_by(Creative.creative_index, Creative.id)).all()
            creative_images = []
            # a restricted account is never touched: not even an image upload
            from .adpolicy import account_problem
            pre = MetaClient(vals["meta_access_token"])
            if problem := account_problem(pre.account(settings_store.ad_account_path(vals["meta_ad_account_id"]))):
                raise MetaError(problem)
            if selected:
                client = pre
                act = settings_store.ad_account_path(vals["meta_ad_account_id"])
                for c in selected:
                    if not c.meta_image_hash or (c.meta_image_account or "") != act:
                        c.meta_image_hash = client.upload_image_bytes(act, (MEDIA_DIR / c.file_name).read_bytes(), c.file_name)
                        c.meta_image_account = act
                        s.commit()
                    creative_images.append({"creative_index": c.creative_index, "image_hash": c.meta_image_hash})
            def save_audiences(aud: dict) -> None:  # reuse these on the next launch of this store
                with get_session() as s2:
                    brands.set_audiences(s2, p.brand_id, aud)
            result = launch_plan(inputs, content, vals, p.id, creative_images, save_audiences)
        except Exception as ex:
            s.rollback()
            p.status = "approved"  # release the claim so the user can try again
            s.commit()
            if isinstance(ex, MetaError):
                raise HTTPException(400, str(ex))
            raise
        try:
            s.add(Launch(plan_id=p.id, campaign_id=result["campaign_id"], objects={**result, "brand_id": p.brand_id}))
            p.status = "launched"
            s.commit()
        except Exception:
            # the campaign exists on Meta but could not be recorded: remove it rather than leave
            # an unmonitored campaign behind
            s.rollback()
            try:
                MetaClient(vals["meta_access_token"]).delete_now(result["campaign_id"])
            except MetaError:
                pass
            p.status = "approved"
            s.commit()
            raise HTTPException(500, "The campaign was created on Meta but could not be saved here, so it was "
                                     "deleted again. Try launching once more.")
        s.refresh(p)
        return _plan_out(p)


# ---------- Monitoring ----------

@app.post("/api/monitor/run")
def monitor_run():
    return monitor.run_once()


@app.get("/api/monitor/overview")
def monitor_overview():
    with get_session() as s:
        launches = s.scalars(select(Launch).order_by(Launch.id.desc())).all()
        out = []
        for l in launches:
            p = s.get(Plan, l.plan_id)
            latest_at = s.scalar(select(MetricSnapshot.taken_at).where(MetricSnapshot.launch_id == l.id)
                                 .order_by(MetricSnapshot.taken_at.desc()).limit(1))
            snaps = s.scalars(select(MetricSnapshot).where(MetricSnapshot.launch_id == l.id,
                                                           MetricSnapshot.taken_at == latest_at)).all() if latest_at else []
            open_alerts = s.scalar(select(func.count()).select_from(Alert)
                                   .where(Alert.launch_id == l.id, Alert.status == "open")) or 0
            po = s.get(PlanOutcome, l.id)
            out.append({
                "outcome": _po_out(po) if po else None,
                "launch_id": l.id, "plan_id": p.id, "product": p.product, "country": p.country,
                "campaign_id": l.campaign_id, "status": l.status, "ads_manager_url": l.objects.get("ads_manager_url"),
                "targets": p.versions[-1].content.get("monitoring_targets", {}),
                "taken_at": latest_at.isoformat() if latest_at else None,
                "open_alerts": open_alerts,
                "metrics": [{"level": m.level, "name": m.name, "status": m.effective_status,
                             "spend_usd": m.spend_usd, "impressions": m.impressions, "clicks": m.clicks,
                             "purchases": m.purchases, "ctr": m.ctr, "cpa_usd": m.cpa_usd,
                             "frequency": m.frequency, "roas": m.roas} for m in snaps],
            })
        return {"state": monitor.state(), "launches": out}


@app.get("/api/alerts")
def list_alerts(status: str = "open"):
    with get_session() as s:
        q = select(Alert).order_by(Alert.updated_at.desc())
        if status != "all":
            q = q.where(Alert.status == status)
        rows = s.scalars(q.limit(200)).all()
        plans = {p.id: p for p in s.scalars(select(Plan).where(Plan.id.in_({a.plan_id for a in rows})))}
        return [{
            "id": a.id, "plan_id": a.plan_id, "launch_id": a.launch_id,
            "product": plans[a.plan_id].product if a.plan_id in plans else "",
            "created_at": a.created_at.isoformat(), "updated_at": a.updated_at.isoformat(),
            "severity": a.severity, "kind": a.kind, "object_level": a.object_level,
            "object_name": a.object_name, "message": a.message, "metrics": a.metrics,
            "suggestions": a.suggestions, "status": a.status, "times_seen": a.times_seen,
        } for a in rows]


class ApplyRequest(BaseModel):
    option_index: int


def _client_for_launch(s, launch_id: int | None) -> MetaClient:
    """The Meta connection of the store this launch belongs to."""
    from . import brands
    launch = s.get(Launch, launch_id) if launch_id else None
    try:
        vals = brands.settings_for(s, brands.launch_brand(s, launch) if launch else 0)
        return MetaClient(vals["meta_access_token"])
    except (MetaError, brands.BrandError) as ex:
        raise HTTPException(400, str(ex))


def _check_account(client: MetaClient, launch: Launch) -> None:
    """A restricted ad account is never changed, not even by a click."""
    from .adpolicy import account_problem
    act = (launch.objects or {}).get("account")
    if not act:
        return
    try:
        problem = account_problem(client.account(act))
    except MetaError as ex:
        raise HTTPException(400, str(ex))
    if problem:
        raise HTTPException(400, problem)


def _log_out(l: ActionLog, oc: ActionOutcome | None = None) -> dict:
    return {"id": l.id, "created_at": l.created_at.isoformat(), "alert_id": l.alert_id, "launch_id": l.launch_id,
            "mode": l.mode, "spec": l.spec, "before": l.before, "after": l.after, "description": l.description,
            "ok": bool(l.ok), "error": l.error, "undone": bool(l.undone),
            "outcome": {"verdict": oc.verdict, "note": oc.note, "before": oc.before, "after": oc.after} if oc else None}


@app.post("/api/alerts/{alert_id}/apply")
def alert_apply(alert_id: int, req: ApplyRequest):
    """Execute one suggested option on Meta (manual). The option must carry a spec."""
    with get_session() as s:
        a = s.get(Alert, alert_id)
        if not a:
            raise HTTPException(404, "Alert not found")
        opts = [o for o in a.suggestions if o.get("action")]
        if not 0 <= req.option_index < len(opts) or not opts[req.option_index].get("spec"):
            raise HTTPException(400, "This option cannot be executed automatically; do it in Ads Manager.")
        if a.status == "resolved":
            raise HTTPException(400, "This alert is resolved; its suggestion no longer applies.")
        launch = s.get(Launch, a.launch_id)
        if not launch or launch.status in ("completed", "archived", "deleted"):
            raise HTTPException(400, "This campaign is closed in the app; change it in Ads Manager if needed.")
        client = _client_for_launch(s, a.launch_id)
        _check_account(client, launch)
        log = actions.execute(s, client, opts[req.option_index]["spec"], "manual", alert_id=a.id, launch_id=a.launch_id)
        if not log.ok:
            raise HTTPException(400, log.error)
        a.status = "acknowledged"
        s.commit()
        return _log_out(log)


@app.get("/api/actions")
def list_actions():
    with get_session() as s:
        rows = s.scalars(select(ActionLog).order_by(ActionLog.id.desc()).limit(100)).all()
        ocs = {o.action_id: o for o in s.scalars(select(ActionOutcome))}
        return [_log_out(l, ocs.get(l.id)) for l in rows]


@app.get("/api/outcomes")
def list_outcomes():
    """Prediction accuracy: per-launch outcomes, per-country calibration, action success rates."""
    with get_session() as s:
        pos = s.scalars(select(PlanOutcome).order_by(PlanOutcome.updated_at.desc())).all()
        plans = {p.id: p for p in s.scalars(select(Plan).where(Plan.id.in_({po.plan_id for po in pos})))}
        cals = s.scalars(select(Calibration)).all()
        return {
            "launches": [{**_po_out(po), "product": plans[po.plan_id].product if po.plan_id in plans else "",
                          "country": plans[po.plan_id].country if po.plan_id in plans else ""} for po in pos],
            "calibration": [{"country": c.country, "name": _cal_name(s, c.country),
                             "factor": c.factor, "samples": c.samples, "updated_at": c.updated_at.isoformat()} for c in cals],
            "action_stats": outcomes.action_stats(s),
        }


def _cal_name(s, key: str) -> str:
    """'US' -> 'United States'; 'PK|2|kitchen_cutlery' -> 'Pakistan · Kitchen, cutlery & dining · Cutlery Nest'."""
    from . import brands
    from .benchmarks import get_category
    parts = key.split("|")
    name = COUNTRY_BENCHMARKS.get(parts[0], {}).get("name", parts[0])
    if len(parts) == 3:
        bid = int(parts[1]) if parts[1].isdigit() else 0
        store = next((b["name"] for b in brands.all_brands(s) if b["id"] == bid), f"store {bid}")
        name += f" · {get_category(parts[2])['label']} · {store}"
    return name


@app.post("/api/launches/{launch_id}/complete")
def complete_launch_now(launch_id: int):
    """Close a launch early: finalise its outcome, update calibration and learn rules."""
    with get_session() as s:
        l = s.get(Launch, launch_id)
        po = s.get(PlanOutcome, launch_id)
        if not l or not po:
            raise HTTPException(404, "Launch or its outcome not found (run a check first)")
        if po.completed:
            raise HTTPException(400, "Already completed")
        if po.spend_usd <= 0:
            raise HTTPException(400, "No spend recorded yet; nothing to learn from")
        outcomes.complete_launch(s, l, s.get(Plan, l.plan_id), po)
        l.status = "archived"
        for a in s.scalars(select(Alert).where(Alert.launch_id == l.id, Alert.status != "resolved")):
            a.status = "resolved"  # nothing is acted on for a closed campaign
        s.commit()
        return _po_out(po)


@app.post("/api/actions/{log_id}/undo")
def action_undo(log_id: int):
    with get_session() as s:
        log = s.get(ActionLog, log_id)
        if not log:
            raise HTTPException(404, "Action not found")
        client = _client_for_launch(s, log.launch_id)
        launch = s.get(Launch, log.launch_id) if log.launch_id else None
        if launch:
            _check_account(client, launch)
        try:
            rev = actions.undo(s, client, log)
        except MetaError as ex:
            raise HTTPException(400, str(ex))
        if not rev.ok:
            raise HTTPException(400, rev.error)
        return _log_out(rev)


class AutomationRequest(BaseModel):
    enabled: dict[str, bool]
    max_budget_pct: float = Field(ge=1, le=50)


@app.get("/api/automation")
def get_automation():
    with get_session() as s:
        return actions.automation_settings(s)


@app.post("/api/automation")
def set_automation(req: AutomationRequest):
    with get_session() as s:
        settings_store.set_many(s, {
            "auto_actions": json.dumps({k: bool(v) for k, v in req.enabled.items() if k in actions.AUTOMATABLE_KINDS}),
            "auto_max_budget_pct": str(req.max_budget_pct),
        })
        return actions.automation_settings(s)


@app.post("/api/alerts/{alert_id}/{action}")
def alert_action(alert_id: int, action: str):
    if action not in ("acknowledge", "resolve", "reopen"):
        raise HTTPException(400, "action must be acknowledge, resolve or reopen")
    with get_session() as s:
        a = s.get(Alert, alert_id)
        if not a:
            raise HTTPException(404, "Alert not found")
        a.status = {"acknowledge": "acknowledged", "resolve": "resolved", "reopen": "open"}[action]
        s.commit()
        return {"id": a.id, "status": a.status}


# ---------- AI creatives ----------

class GenerateRequest(BaseModel):
    creative_index: int = 0
    aspect: str | None = None  # 1:1 | 9:16 | 4:5 (default from the creative's format)


def _brand_vals(s, brand_id: int) -> dict:
    from . import brands
    try:
        return brands.settings_for(s, brand_id)
    except brands.BrandError as ex:
        raise HTTPException(404, str(ex))


@app.get("/api/creatives/status")
def creatives_status(brand_id: int = 0):
    from . import brands
    with get_session() as s:
        try:
            vals = brands.settings_for(s, brand_id)
        except brands.BrandError:
            vals = {}  # the plan's store was deleted: show an empty photo box instead of failing the panel
    st = imagegen.status()
    st["product_photo"] = f"/media/{vals['product_photo']}" if vals.get("product_photo") else ""
    st["brand_style"] = vals.get("brand_style", "")
    return st


@app.post("/api/creatives/product-photo")
async def upload_product_photo(file: UploadFile = File(...), brand_id: int = 0):
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(400, "Image too large (max 15 MB)")
    ext = creatives.image_kind(data)
    if not ext:
        raise HTTPException(400, "Use a PNG, JPG or WEBP image")
    from . import brands
    with get_session() as s:
        old = _brand_vals(s, brand_id).get("product_photo")  # 404 before anything is written
    name = f"product_{secrets.token_urlsafe(16)}.{ext}"
    (MEDIA_DIR / name).write_bytes(data)
    with get_session() as s:
        try:
            brands.set_fields(s, brand_id, {"product_photo": name})
        except brands.BrandError as ex:
            creatives.delete_file(name)
            raise HTTPException(404, str(ex))
        if old:
            creatives.delete_file(old)
    return {"product_photo": f"/media/{name}"}


class BrandStyleRequest(BaseModel):
    brand_style: str = Field(default="", max_length=1000)


@app.post("/api/creatives/brand-style")
def set_brand_style(req: BrandStyleRequest, brand_id: int = 0):
    from . import brands
    with get_session() as s:
        try:
            brands.set_fields(s, brand_id, {"brand_style": req.brand_style})
        except brands.BrandError as ex:
            raise HTTPException(404, str(ex))
    return {"ok": True}


@app.get("/api/plans/{plan_id}/creatives")
def list_creatives(plan_id: int):
    with get_session() as s:
        rows = s.scalars(select(Creative).where(Creative.plan_id == plan_id)
                         .order_by(Creative.creative_index, Creative.id)).all()
        return [creatives.creative_out(c) for c in rows]


@app.post("/api/plans/{plan_id}/creatives/generate")
def generate_creative(plan_id: int, req: GenerateRequest):
    with get_session() as s:
        p = s.get(Plan, plan_id)
        if not p:
            raise HTTPException(404, "Plan not found")
        try:
            row = creatives.generate_for_plan(s, p, req.creative_index, req.aspect)
        except (imagegen.ImageGenError, ValueError) as ex:
            raise HTTPException(400, str(ex))
        return creatives.creative_out(row)


@app.post("/api/plans/{plan_id}/creatives/upload")
async def upload_creative(plan_id: int, creative_index: int = 0, file: UploadFile = File(...)):
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(400, "Image too large (max 15 MB)")
    ext = creatives.image_kind(data)
    if not ext:
        raise HTTPException(400, "Use a PNG, JPG or WEBP image")
    with get_session() as s:
        p = s.get(Plan, plan_id)
        if not p:
            raise HTTPException(404, "Plan not found")
        return creatives.creative_out(creatives.save_upload(s, p, creative_index, data, ext))


@app.post("/api/creatives/{creative_id}/toggle")
def toggle_creative(creative_id: int):
    with get_session() as s:
        c = s.get(Creative, creative_id)
        if not c:
            raise HTTPException(404, "Creative not found")
        c.selected = 0 if c.selected else 1
        s.commit()
        return creatives.creative_out(c)


@app.delete("/api/creatives/{creative_id}")
def delete_creative(creative_id: int):
    with get_session() as s:
        c = s.get(Creative, creative_id)
        if not c:
            raise HTTPException(404, "Creative not found")
        creatives.delete_file(c.file_name)
        s.delete(c)
        s.commit()
        return {"ok": True}


# ---------- Connections: Higgsfield (images) and Gmail (alerts), loopback paste flow ----------

class PastedUrl(BaseModel):
    url: str = Field(min_length=10, max_length=4000)


@app.get("/api/higgsfield/status")
def hf_status():
    from .config import IMAGE_PROVIDER
    return {**higgsfield.status(), "in_use": IMAGE_PROVIDER == "higgsfield"}


@app.post("/api/higgsfield/connect")
def hf_connect(request: Request):
    try:
        return {"url": higgsfield.start_login(request.state.user["id"])}
    except (higgsfield.HiggsfieldError, requests.RequestException) as ex:
        raise HTTPException(502, str(ex))


@app.post("/api/higgsfield/complete")
def hf_complete(req: PastedUrl, request: Request):
    try:
        return {"connected": True, "email": higgsfield.complete_login(req.url, request.state.user["id"])}
    except (higgsfield.HiggsfieldError, requests.RequestException) as ex:
        raise HTTPException(400, str(ex))


@app.post("/api/higgsfield/disconnect")
def hf_disconnect():
    higgsfield.disconnect()
    return {"connected": False}


@app.post("/api/gmail/connect")
def gmail_connect(request: Request):
    try:
        return {"url": gmail_auth.start_login(request.state.user["id"])}
    except gmail_auth.GmailAuthError as ex:
        raise HTTPException(400, str(ex))


@app.post("/api/gmail/complete")
def gmail_complete(req: PastedUrl, request: Request):
    try:
        return {"connected": True, "email": gmail_auth.complete_login(req.url, request.state.user["id"])}
    except (gmail_auth.GmailAuthError, requests.RequestException) as ex:
        raise HTTPException(400, str(ex))


@app.post("/api/gmail/disconnect")
def gmail_disconnect():
    gmail_auth.disconnect()
    return {"ok": True}


# ---------- Today ----------

@app.get("/api/today")
def today_view():
    from . import today
    return today.build()


# ---------- Activity log ----------

@app.get("/api/activity")
def list_activity(kind: str = "", level: str = "", q: str = "", before_id: int = 0, limit: int = 100):
    from .db import ActivityLog
    limit = max(1, min(limit, 200))
    with get_session() as s:
        query = select(ActivityLog).order_by(ActivityLog.id.desc())
        if kind:
            query = query.where(ActivityLog.kind == kind)
        if level:
            query = query.where(ActivityLog.level == level)
        if q:
            like = f"%{q.strip()}%"
            query = query.where(ActivityLog.title.ilike(like) | ActivityLog.detail.ilike(like))
        if before_id:
            query = query.where(ActivityLog.id < before_id)
        rows = s.scalars(query.limit(limit + 1)).all()
        return {"items": [activity.out(a) for a in rows[:limit]], "more": len(rows) > limit,
                "kinds": list(activity.KINDS)}


# ---------- Payments: AI usage, budget, Meta ad spend ----------

class BillingRequest(BaseModel):
    budget_usd: float = Field(ge=0, le=10000)
    prices: dict[str, dict[str, float]] | None = None


@app.get("/api/billing")
def billing():
    out = usage.summary()
    with get_session() as s:
        ads = []
        for l in s.scalars(select(Launch).order_by(Launch.id.desc())):
            p = s.get(Plan, l.plan_id)
            po = s.get(PlanOutcome, l.id)
            ads.append({
                "launch_id": l.id, "plan_id": p.id, "product": p.product, "country": p.country, "status": l.status,
                "budget_pkr": p.budget_pkr, "budget_usd": round(p.budget_pkr / fx.launch_rate(l, p), 2),
                "spend_usd": po.spend_usd if po else 0, "purchases": po.purchases if po else 0,
                "revenue_usd": po.revenue_usd if po else 0, "days_active": po.days_active if po else 0,
                "duration_days": p.duration_days, "created_at": l.created_at.isoformat(),
            })
    out["ads"] = ads
    out["ad_spend_usd"] = round(sum(a["spend_usd"] for a in ads), 2)
    out["ad_budget_usd"] = round(sum(a["budget_usd"] for a in ads if a["status"] not in ("completed", "archived", "deleted")), 2)
    out["ad_revenue_usd"] = round(sum(a["revenue_usd"] for a in ads), 2)
    out["pkr_per_usd"] = fx.pkr_per_usd()
    return out


@app.post("/api/billing")
def save_billing(req: BillingRequest):
    usage.set_budget(req.budget_usd)
    if req.prices is not None:
        usage.set_prices(req.prices)
    return billing()


# ---------- Notifications ----------

@app.get("/api/notify/status")
def notify_status():
    return notify.status()


@app.post("/api/notify/test")
def notify_test():
    try:
        via = notify.send_test_email()
    except Exception as ex:
        raise HTTPException(400, str(ex))
    return {"ok": True, "to": notify.status()["to"], "via": via}


@app.post("/api/notify/digest")
def notify_digest_now():
    with get_session() as s:
        try:
            r = notify.send_digest(s)
        except Exception as ex:
            raise HTTPException(400, str(ex))
        settings_store.set_many(s, {"last_digest_at": __import__("datetime").date.today().isoformat()})
    return r


@app.get("/api/notify/digest/preview")
def notify_digest_preview():
    """The digest as JSON, so the UI can show what would be emailed."""
    with get_session() as s:
        return notify.digest_data(s)


# ---------- frontend ----------

app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
app.mount("/media", StaticFiles(directory=MEDIA_DIR), name="media")


@app.get("/")
def index():
    return FileResponse(FRONTEND_DIR / "index.html")
