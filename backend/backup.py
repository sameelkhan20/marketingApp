"""Daily copies of the database, kept in the data volume (so they survive rebuilds), the last 14 kept.

SQLite's online backup API makes a consistent copy while the app keeps writing. The owner can also
download any copy from the Payments page to keep one off the server. Secrets inside are encrypted
with a key that is not in the database, so a copy alone does not expose the Meta or Google tokens.
"""

import logging
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from .config import DATA_DIR

log = logging.getLogger("backup")
DIR = DATA_DIR / "backups"
KEEP = 14
NAME = re.compile(r"^app-\d{8}-\d{6}\.db$")
_lock = threading.Lock()


def run() -> dict:
    with _lock:
        DIR.mkdir(parents=True, exist_ok=True)
        name = f"app-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.db"
        dest = DIR / name
        src = sqlite3.connect(DATA_DIR / "app.db", timeout=30)
        try:
            out = sqlite3.connect(dest)
            try:
                src.backup(out)
            finally:
                out.close()
        finally:
            src.close()
        for old in list_all()[KEEP:]:
            (DIR / old["name"]).unlink(missing_ok=True)
        log.info("backup written: %s", name)
        return {"name": name, "bytes": dest.stat().st_size}


def list_all() -> list[dict]:
    if not DIR.exists():
        return []
    rows = [p for p in DIR.iterdir() if NAME.match(p.name)]
    rows.sort(key=lambda p: p.name, reverse=True)
    return [{"name": p.name, "bytes": p.stat().st_size,
             "created_at": datetime.strptime(p.name[4:19], "%Y%m%d-%H%M%S").isoformat()} for p in rows]


def path_of(name: str) -> Path | None:
    """Only names this module wrote, never a path from the request."""
    if not NAME.match(name or ""):
        return None
    p = DIR / name
    return p if p.is_file() else None


def last_age_hours() -> float | None:
    rows = list_all()
    if not rows:
        return None
    return (datetime.utcnow() - datetime.fromisoformat(rows[0]["created_at"])).total_seconds() / 3600


def start_scheduler() -> None:
    """A copy shortly after start when none is recent, then every 24 hours."""
    def loop():
        try:
            age = last_age_hours()
            if age is None or age >= 23:
                run()
        except Exception as ex:
            log.exception("backup failed: %s", ex)
        t = threading.Timer(3600, loop)  # check hourly; writes at most once a day
        t.daemon = True
        t.start()

    t = threading.Timer(60, loop)
    t.daemon = True
    t.start()
