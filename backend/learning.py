"""Learning loop: turn user edits/approvals into reusable strategy rules.

Two layers:
1. Deterministic diff of the AI draft vs the user's version (what changed).
2. Claude turns the diff + the user's note into 1-3 short, generalisable rules.
   Without an API key, the user's note itself becomes the rule.

Rules live in the learned_rules table and are injected into every new plan.
"""

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import LearnedRule


MAX_RULES_IN_PROMPT = 15
RETIRE_BELOW = 0.4


def applies_to(rule: str, country: str | None, niche: str | None) -> bool:
    """'[result GB leather_jackets] ...' rules only shape plans for that country and niche."""
    if not rule.startswith("[result ") or not country:
        return True
    tag = rule[len("[result "):].split("]", 1)[0].split()
    return len(tag) == 2 and tag[0].upper() == country.upper() and (not niche or tag[1] == niche)


def active_rules(session: Session, country: str | None = None, niche: str | None = None,
                 limit: int | None = None) -> list[str]:
    rows = session.scalars(
        select(LearnedRule).where(LearnedRule.active == 1).order_by(LearnedRule.weight.desc(), LearnedRule.id.desc())
    ).all()
    out = [r.rule for r in rows if applies_to(r.rule, country, niche)]
    return out[:limit] if limit else out


def diff_plans(ai: dict, user: dict) -> list[str]:
    """Human-readable list of top-level changes between two plan dicts."""
    changes: list[str] = []
    for key in user:
        if key not in ai:
            changes.append(f"added '{key}'")
        elif ai[key] != user[key]:
            changes.append(f"changed '{key}': {_short(ai[key])} -> {_short(user[key])}")
    for key in ai:
        if key not in user:
            changes.append(f"removed '{key}'")
    return changes


def _short(v, n: int = 300) -> str:
    s = json.dumps(v, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + "..."


def learn_from_edit(session: Session, plan_id: int, previous_plan: dict, user_plan: dict, note: str) -> list[str]:
    """previous_plan is the version this edit replaced, so each edit is learned once."""
    changes = diff_plans(previous_plan, user_plan)
    if not changes and not note.strip():
        return []

    existing = {r.lower().strip() for r in active_rules(session)}
    rules = [r for r in _extract_rules(changes, note) if r.lower().strip() not in existing]
    for r in rules:
        session.add(LearnedRule(rule=r, source_plan_id=plan_id))
    session.commit()
    return rules


def rules_in_effect_for(session: Session, plan_created_at) -> list[LearnedRule]:
    """Active rules that existed when a plan was generated (the ones that shaped it)."""
    return list(session.scalars(select(LearnedRule).where(LearnedRule.active == 1,
                                                          LearnedRule.created_at <= plan_created_at)))


def reinforce_on_approval(session: Session, plan_id: int) -> None:
    """Approved without edits: the rules that shaped this plan worked, bump their weight a little."""
    from .db import Plan
    plan = session.get(Plan, plan_id)
    for r in rules_in_effect_for(session, plan.created_at):
        if applies_to(r.rule, plan.country, plan.niche):
            r.weight = round(min(3.0, r.weight + 0.1), 2)
    session.commit()


# Free-text fields: editing them says nothing general about strategy.
_NOT_LEARNABLE = ("'summary'", "'risks'", "'objective'")


def _extract_rules(changes: list[str], note: str) -> list[str]:
    from . import llm, usage
    fallback = [note.strip()] if note.strip() else []
    if not llm.configured():
        return fallback or [f"User prefers: {c}" for c in changes if not any(k in c for k in _NOT_LEARNABLE)][:2]
    schema = {
        "type": "object",
        "properties": {"rules": {"type": "array", "items": {"type": "string"}}},
        "required": ["rules"],
        "additionalProperties": False,
    }
    prompt = (
        "A Meta Ads planner produced a draft and the user edited it.\n\n"
        "CHANGES:\n" + "\n".join(f"- {c}" for c in changes) + "\n\n"
        f"USER NOTE: {note or '(none)'}\n\n"
        "Write 1-3 short, general rules (one sentence each) the planner should follow "
        "next time so the user would not need to make these edits again. "
        "Only rules that generalise beyond this one product."
    )
    try:
        rules = llm.structured("You turn a marketer's plan edits into reusable planning rules.", prompt, schema,
                               "Learning", effort="medium", max_tokens=2000)["rules"]
    except (usage.BudgetExceeded, llm.LLMError):
        return fallback
    return [str(r).strip() for r in rules if str(r).strip()][:3] or fallback
