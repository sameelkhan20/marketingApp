"""Activity log: one line per thing that happened. Recording never breaks the real work."""

import logging

from .db import ActivityLog, get_session

log = logging.getLogger("activity")

KINDS = ("auth", "plan", "launch", "monitor", "action", "email", "image", "settings", "learning")
LEVELS = ("info", "ok", "warning", "error")


def record(kind: str, action: str, title: str, detail: str = "", level: str = "info", actor: str = "") -> None:
    try:
        with get_session() as s:
            s.add(ActivityLog(kind=kind, action=action, title=title[:500], detail=(detail or "")[:4000],
                              level=level if level in LEVELS else "info", actor=actor or "system"))
            s.commit()
    except Exception as ex:  # the log must never stop a launch, an email or a login
        log.warning("activity not recorded: %s", ex)


def out(a: ActivityLog) -> dict:
    return {"id": a.id, "created_at": a.created_at.isoformat(), "kind": a.kind, "action": a.action,
            "level": a.level, "title": a.title, "detail": a.detail, "actor": a.actor}
