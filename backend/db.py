from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship

from .config import DB_URL

from sqlalchemy import event

# The monitor thread and requests share this DB: wait for locks instead of failing fast,
# and use WAL so readers never block on a writer.
engine = create_engine(DB_URL, connect_args={"check_same_thread": False, "timeout": 30})


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _record):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=30000")
    cur.close()


class Base(DeclarativeBase):
    pass


class Plan(Base):
    """One planning request. Versions hold the AI draft and every user edit."""

    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft | approved | launched
    # inputs
    budget_pkr: Mapped[float] = mapped_column(Float)
    country: Mapped[str] = mapped_column(String(4))
    product: Mapped[str] = mapped_column(Text)
    niche: Mapped[str] = mapped_column(String(40))  # product category key (backend/benchmarks.py)
    category_note: Mapped[str] = mapped_column(Text, default="")  # the user's words for the product type
    brand_id: Mapped[int] = mapped_column(Integer, default=0)      # which store's Meta connection (backend/brands.py)
    brief: Mapped[dict] = mapped_column(JSON, default=dict)          # offer, why buy, delivery, returns, reviews
    price_usd: Mapped[float] = mapped_column(Float)
    duration_days: Mapped[int] = mapped_column(Integer)
    # estimate snapshot
    estimate: Mapped[dict] = mapped_column(JSON)

    versions: Mapped[list["PlanVersion"]] = relationship(
        back_populates="plan", cascade="all, delete-orphan", order_by="PlanVersion.version"
    )
    launches: Mapped[list["Launch"]] = relationship(order_by="Launch.id")


class PlanVersion(Base):
    __tablename__ = "plan_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id"))
    version: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(10))  # ai | user
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    content: Mapped[dict] = mapped_column(JSON)
    note: Mapped[str] = mapped_column(Text, default="")  # user's reason for the change

    plan: Mapped[Plan] = relationship(back_populates="versions")


class LearnedRule(Base):
    """What the planner has learned from the user's edits and approvals."""

    __tablename__ = "learned_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    rule: Mapped[str] = mapped_column(Text)
    source_plan_id: Mapped[int] = mapped_column(Integer, nullable=True)
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    active: Mapped[int] = mapped_column(Integer, default=1)
    niche: Mapped[str] = mapped_column(String(40), default="")  # the category it was learned for; "" = every plan
    brand_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # the store; None = every store


class Setting(Base):
    """Key/value settings edited from the UI (Meta token, account, page, pixel...)."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class AccountStat(Base):
    """Aggregated Insights per country from the connected ad account."""

    __tablename__ = "account_stats"

    country: Mapped[str] = mapped_column(String(4), primary_key=True)
    since: Mapped[str] = mapped_column(String(10))
    until: Mapped[str] = mapped_column(String(10))
    spend_usd: Mapped[float] = mapped_column(Float)
    impressions: Mapped[int] = mapped_column(Integer)
    clicks: Mapped[int] = mapped_column(Integer)
    purchases: Mapped[int] = mapped_column(Integer)
    revenue_usd: Mapped[float] = mapped_column(Float, default=0)
    cpm_usd: Mapped[float] = mapped_column(Float)
    ctr: Mapped[float] = mapped_column(Float)
    cvr: Mapped[float] = mapped_column(Float)
    confidence: Mapped[str] = mapped_column(String(10))
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class StoreStat(Base):
    """Same as AccountStat, for each store added under Stores (brand_id >= 1), from that store's own ad
    account. The main store (brand 0) keeps using account_stats, so its synced history is untouched."""

    __tablename__ = "store_stats"

    brand_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    country: Mapped[str] = mapped_column(String(4), primary_key=True)
    since: Mapped[str] = mapped_column(String(10))
    until: Mapped[str] = mapped_column(String(10))
    spend_usd: Mapped[float] = mapped_column(Float)
    impressions: Mapped[int] = mapped_column(Integer)
    clicks: Mapped[int] = mapped_column(Integer)
    purchases: Mapped[int] = mapped_column(Integer)
    revenue_usd: Mapped[float] = mapped_column(Float, default=0)
    cpm_usd: Mapped[float] = mapped_column(Float)
    ctr: Mapped[float] = mapped_column(Float)
    cvr: Mapped[float] = mapped_column(Float)
    confidence: Mapped[str] = mapped_column(String(10))
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


def stat_rows(session, brand_id: int = 0) -> list:
    """The synced history of one store, biggest spend first."""
    from sqlalchemy import select
    if brand_id:
        q = select(StoreStat).where(StoreStat.brand_id == int(brand_id))
        return list(session.scalars(q.order_by(StoreStat.spend_usd.desc())))
    return list(session.scalars(select(AccountStat).order_by(AccountStat.spend_usd.desc())))


def stat_for(session, brand_id: int, country: str):
    """One store's history for one country, or None."""
    if brand_id:
        return session.get(StoreStat, (int(brand_id), country.upper()))
    return session.get(AccountStat, country.upper())


class Launch(Base):
    """A plan pushed to Meta. Everything is created PAUSED; the user activates in Ads Manager."""

    __tablename__ = "launches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    campaign_id: Mapped[str] = mapped_column(String(40))
    objects: Mapped[dict] = mapped_column(JSON)  # {adsets: [...], ads: [...], skipped: [...]}
    status: Mapped[str] = mapped_column(String(20), default="paused")


class MetricSnapshot(Base):
    """One monitoring read of one Meta object (campaign / adset / ad), last-7-day window."""

    __tablename__ = "metric_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    launch_id: Mapped[int] = mapped_column(ForeignKey("launches.id"))
    taken_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    level: Mapped[str] = mapped_column(String(10))  # campaign | adset | ad
    object_id: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(Text)
    effective_status: Mapped[str] = mapped_column(String(30), default="")
    spend_usd: Mapped[float] = mapped_column(Float, default=0)
    impressions: Mapped[int] = mapped_column(Integer, default=0)
    clicks: Mapped[int] = mapped_column(Integer, default=0)
    purchases: Mapped[int] = mapped_column(Integer, default=0)
    revenue_usd: Mapped[float] = mapped_column(Float, default=0)
    frequency: Mapped[float] = mapped_column(Float, default=0)
    ctr: Mapped[float] = mapped_column(Float, default=0)
    cpa_usd: Mapped[float] = mapped_column(Float, default=0)
    roas: Mapped[float] = mapped_column(Float, default=0)


class Alert(Base):
    """A deviation from the plan, with suggested actions. Deduped by fingerprint while open."""

    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    launch_id: Mapped[int] = mapped_column(ForeignKey("launches.id"))
    plan_id: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    fingerprint: Mapped[str] = mapped_column(String(120))
    severity: Mapped[str] = mapped_column(String(10))  # info | warning | critical
    kind: Mapped[str] = mapped_column(String(30))
    object_level: Mapped[str] = mapped_column(String(10))
    object_id: Mapped[str] = mapped_column(String(40))
    object_name: Mapped[str] = mapped_column(Text)
    message: Mapped[str] = mapped_column(Text)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    suggestions: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(15), default="open")  # open | acknowledged | resolved
    notified: Mapped[int] = mapped_column(Integer, default=0)
    times_seen: Mapped[int] = mapped_column(Integer, default=1)


class PlanOutcome(Base):
    """Predicted vs actual for a launch, refreshed on every monitor run; finalised at completion."""

    __tablename__ = "plan_outcomes"

    launch_id: Mapped[int] = mapped_column(ForeignKey("launches.id"), primary_key=True)
    plan_id: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    days_active: Mapped[int] = mapped_column(Integer, default=0)
    duration_days: Mapped[int] = mapped_column(Integer, default=0)
    spend_usd: Mapped[float] = mapped_column(Float, default=0)
    purchases: Mapped[int] = mapped_column(Integer, default=0)
    revenue_usd: Mapped[float] = mapped_column(Float, default=0)
    predicted_expected: Mapped[int] = mapped_column(Integer, default=0)
    predicted_low: Mapped[int] = mapped_column(Integer, default=0)
    predicted_high: Mapped[int] = mapped_column(Integer, default=0)
    predicted_so_far: Mapped[float] = mapped_column(Float, default=0)  # expected prorated by days active
    ratio: Mapped[float] = mapped_column(Float, default=0)             # actual / predicted_so_far
    verdict: Mapped[str] = mapped_column(String(15), default="")       # ahead | on_track | behind | no_data
    completed: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[str] = mapped_column(Text, default="")
    rules_added: Mapped[list] = mapped_column(JSON, default=list)


class ActionOutcome(Base):
    """What happened in the days after an action: the object's key metric before vs after."""

    __tablename__ = "action_outcomes"

    action_id: Mapped[int] = mapped_column(ForeignKey("action_log.id"), primary_key=True)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    metric: Mapped[str] = mapped_column(String(20))
    before: Mapped[float] = mapped_column(Float, default=0)
    after: Mapped[float] = mapped_column(Float, default=0)
    purchases_before: Mapped[int] = mapped_column(Integer, default=0)
    purchases_after: Mapped[int] = mapped_column(Integer, default=0)
    verdict: Mapped[str] = mapped_column(String(15))  # improved | neutral | worse | no_data
    note: Mapped[str] = mapped_column(Text, default="")


class Calibration(Base):
    """Per-country correction applied to estimates: factor = observed actual / predicted."""

    __tablename__ = "calibration"

    country: Mapped[str] = mapped_column(String(4), primary_key=True)
    factor: Mapped[float] = mapped_column(Float, default=1.0)
    samples: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Creative(Base):
    """An ad image for a plan: AI-generated or uploaded. Selected ones go to Meta on launch."""

    __tablename__ = "creatives"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    source: Mapped[str] = mapped_column(String(10))  # ai | upload
    provider: Mapped[str] = mapped_column(String(20), default="")
    creative_index: Mapped[int] = mapped_column(Integer, default=0)  # which plan creative (copy) it belongs to
    format: Mapped[str] = mapped_column(String(10), default="1:1")   # 1:1 | 9:16 | 4:5
    prompt: Mapped[str] = mapped_column(Text, default="")
    file_name: Mapped[str] = mapped_column(String(120))
    selected: Mapped[int] = mapped_column(Integer, default=1)
    meta_image_hash: Mapped[str] = mapped_column(String(80), default="")
    meta_image_account: Mapped[str] = mapped_column(String(40), default="")  # the account the hash belongs to


class ActionLog(Base):
    """Every change the app made on Meta (manual Apply or auto), with the state before it for Undo."""

    __tablename__ = "action_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    alert_id: Mapped[int] = mapped_column(Integer, nullable=True)
    launch_id: Mapped[int] = mapped_column(Integer, nullable=True)
    mode: Mapped[str] = mapped_column(String(10))  # manual | auto | undo
    spec: Mapped[dict] = mapped_column(JSON)       # {type, level, object_id, pct?}
    before: Mapped[dict] = mapped_column(JSON, default=dict)
    after: Mapped[dict] = mapped_column(JSON, default=dict)
    description: Mapped[str] = mapped_column(Text, default="")
    ok: Mapped[int] = mapped_column(Integer, default=1)
    error: Mapped[str] = mapped_column(Text, default="")
    undone: Mapped[int] = mapped_column(Integer, default=0)


class ActivityLog(Base):
    """Everything that happened in the app, newest first on the Activity page."""

    __tablename__ = "activity_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    kind: Mapped[str] = mapped_column(String(20), index=True)   # auth | plan | launch | monitor | action | email | image | settings | learning
    action: Mapped[str] = mapped_column(String(40))
    level: Mapped[str] = mapped_column(String(10), default="info")  # info | ok | warning | error
    title: Mapped[str] = mapped_column(Text)
    detail: Mapped[str] = mapped_column(Text, default="")
    actor: Mapped[str] = mapped_column(String(255), default="")  # user email, or "monitor" / "system"


class ApiUsage(Base):
    """One paid API call (Claude text or an AI image) with its estimated cost."""

    __tablename__ = "api_usage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    provider: Mapped[str] = mapped_column(String(30))   # anthropic | higgsfield | gemini | openai
    model: Mapped[str] = mapped_column(String(80), default="")
    feature: Mapped[str] = mapped_column(String(40))    # Plans | Learning | Alert advice | Result rules | Ad images
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    images: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0)
    ok: Mapped[int] = mapped_column(Integer, default=1)
    error: Mapped[str] = mapped_column(Text, default="")


class User(Base):
    """Ported from core_auth_fixed (users table), SQLite instead of PostgreSQL."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Integer, default=True)
    is_superuser: Mapped[bool] = mapped_column(Integer, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # bumped on password change: every token issued before it stops working
    token_version: Mapped[int] = mapped_column(Integer, default=0)


class TokenBlacklist(Base):
    """Replaces the Redis blacklist: revoked access tokens until they expire."""

    __tablename__ = "token_blacklist"

    token: Mapped[str] = mapped_column(String(600), primary_key=True)
    expires_at: Mapped[int] = mapped_column(Integer, index=True)  # unix seconds


def init_db() -> None:
    Base.metadata.create_all(engine)
    _add_missing_columns()


# create_all never alters existing tables: add columns introduced after the first deploy.
_NEW_COLUMNS = [("users", "token_version", "INTEGER NOT NULL DEFAULT 0"),
                ("plans", "category_note", "TEXT NOT NULL DEFAULT ''"),
                ("plans", "brand_id", "INTEGER NOT NULL DEFAULT 0"),
                ("learned_rules", "niche", "VARCHAR(40) NOT NULL DEFAULT ''"),
                ("plans", "brief", "TEXT NOT NULL DEFAULT '{}'"),
                ("creatives", "meta_image_account", "VARCHAR(40) NOT NULL DEFAULT ''"),
                ("learned_rules", "brand_id", "INTEGER")]


def _add_missing_columns() -> None:
    import hashlib

    from sqlalchemy import text
    with engine.begin() as conn:
        for table, column, ddl in _NEW_COLUMNS:
            have = {r[1] for r in conn.execute(text(f"PRAGMA table_info({table})"))}
            if column not in have:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
                if (table, column) == ("learned_rules", "brand_id"):
                    conn.execute(text("UPDATE learned_rules SET brand_id = (SELECT p.brand_id FROM plans p "
                                      "WHERE p.id = learned_rules.source_plan_id) WHERE source_plan_id IS NOT NULL"))
                if (table, column) == ("learned_rules", "niche"):
                    # rules learned before categories existed came from jacket plans: keep them there
                    conn.execute(text("UPDATE learned_rules SET niche = COALESCE((SELECT p.niche FROM plans p "
                                      "WHERE p.id = learned_rules.source_plan_id), '') WHERE source_plan_id IS NOT NULL"))
        # a rule tagged with a category that no longer exists (older names) applies to every plan again
        from .benchmarks import CATEGORIES
        keys = ",".join(f"'{k}'" for k in CATEGORIES)
        conn.execute(text(f"UPDATE learned_rules SET niche = '' WHERE niche <> '' AND niche NOT IN ({keys})"))
        # emails are kept in lower case (unless that would clash with another account)
        conn.execute(text("UPDATE users SET email = lower(email) WHERE email <> lower(email) AND NOT EXISTS "
                          "(SELECT 1 FROM users u2 WHERE u2.id <> users.id AND lower(u2.email) = lower(users.email))"))
        # revoked tokens used to be stored whole; they are now looked up by their SHA-256
        for (tok,) in conn.execute(text("SELECT token FROM token_blacklist WHERE length(token) > 64")).fetchall():
            h = hashlib.sha256(tok.encode()).hexdigest()
            conn.execute(text("DELETE FROM token_blacklist WHERE token = :h"), {"h": h})
            conn.execute(text("UPDATE token_blacklist SET token = :h WHERE token = :t"), {"h": h, "t": tok})


def get_session() -> Session:
    return Session(engine)
