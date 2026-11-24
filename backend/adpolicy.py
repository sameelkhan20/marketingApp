"""Ad copy checks against Meta's Advertising Standards, before anything reaches Meta.

Rejected ads lower an ad account's quality, and repeated rejections get accounts restricted. The
patterns that most often get Pakistan-based stores banned are counterfeit wording ("replica",
"first copy", "7A quality"), famous brand names, personal attributes ("Are you overweight?") and
health or income claims. "block" findings stop a launch; "warn" findings are shown so the owner
can decide.

The patterns are kept narrow on purpose: "all black jacket", "coach jacket", "faux leather",
"off-white", "slim fit", "a single click away" and "cures boredom" are ordinary words and must not
be flagged.

This is a safety net, not legal advice: Meta's own review still has the last word.
"""

from __future__ import annotations

import re

# Selling fakes is the fastest way to a permanent ban.
_COUNTERFEIT = re.compile(
    r"\b(replica|(?:1st|first)[\s-]?copy|master[\s-]?copy|1:1 copy|(?:aaa\+?|\d{1,2}a)\s*(?:quality|grade)|"
    r"mirror[\s-]?quality|dupe(?![-\w])|knock[\s-]?off)", re.I)
# Famous marks: using them in copy reads as counterfeit or trademark misuse ("compatible with" is fine).
_BRANDS = re.compile(
    r"\b(n[i1]ke|adidas|gucci|louis[\s-]?vuitton|lv|ysl|saint[\s-]?laurent|chanel|rolex|prada|herm[eè]s|dior|versace|"
    r"balenciaga|burberry|fendi|givenchy|bottega[\s-]?veneta|cartier|tag[\s-]?heuer|armani|hugo[\s-]?boss|"
    r"ralph[\s-]?lauren|tommy[\s-]?hilfiger|levi'?s|zara|allsaints|schott|belstaff|barbour|moncler|"
    r"canada[\s-]?goose|north[\s-]?face|timberland|dr\.?[\s-]?martens|harley[\s-]?davidson|ray[\s-]?ban|"
    r"michael[\s-]?kors|disney|star[\s-]?wars|harry[\s-]?potter)\b", re.I)
_COMPATIBLE = re.compile(r"\b(compatible with|for use with|works with)\b", re.I)
# Films, shows and people: the celebrity niche's biggest risk (likeness and trademark).
_SCREEN = re.compile(r"\b(worn by|as (?:seen|worn) (?:on|in|by)|from the (?:movie|film|show|series|tv show))\b", re.I)
_TITLE_AFTER_FROM = re.compile(r"\b(?:from|in|like)\s+(?:the\s+)?([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b")
# Personal attributes: copy may not assert the reader's traits ("Are you overweight?").
_PERSONAL = re.compile(
    r"\b(?:are you|you are|you're|aren't you|if you're|if you are|fellow)\s+(?:still\s+|so\s+|feeling\s+|an?\s+)?("
    r"overweight|fat|obese|bald|balding|single(?!\s+(?:click|step|tap|piece|strap|layer|size|colou?r|order|item|pair))|"
    r"divorced|depressed|anxious|diabetic|disabled|pregnant|broke|in debt|unemployed|gay|lesbian|bisexual|"
    r"muslims?|christians?|hindus?|jewish|jews|sikhs?|elderly|over \d{2}|under \d{2})(?![-\w])", re.I)
_HEALTH = re.compile(
    r"\b((?:cures?|heals?|treats?)\s+(?:acne|eczema|psoriasis|hair loss|baldness|diabetes|pain|arthritis|"
    r"wrinkles|anxiety|depression|insomnia|cancer)|(?:struggling with|suffering from|tired of being|ashamed of)\s+"
    r"(?:\w+\s+){0,2}?(?:hair loss|acne|wrinkles|weight|fat|overweight|bald|anxiety|depression|pain)|"
    r"lose (?:\d+\s*(?:kg|kgs|lbs|pounds)|weight)|weight[\s-]?loss|slim(?:ming| down)|burn (?:belly )?fat|"
    r"fat[\s-]?burn(?:er|ing)|(?:re)?grows? (?:your )?hair|hair (?:re)?growth|boosts? (?:your )?immunity|detox|"
    r"miracle|clinically proven|doctor[\s-]?(?:recommended|approved)|fda[\s-]?approved|before (?:and|&) after)\b", re.I)
_INCOME = re.compile(
    r"\b(get rich|passive income|guaranteed (?:income|profit|returns)|"
    r"(?:earn|make)\s+(?:rs\.?|pkr|usd|\$)?\s?\d[\d,]*k?\s+(?:a|per|every)\s+(?:day|week|month))\b", re.I)
_ABSOLUTE = re.compile(r"(\bguaranteed\b|\b100% (?:guaranteed|safe|effective)\b|#1\b|\bnumber one\b|\bbest in the world\b|"
                       r"\brisk[\s-]?free\b)", re.I)
_SCARCITY = re.compile(r"\b(only \d+ left|last \d+ (?:pieces|items|units)|ends (?:tonight|today)|today only|"
                       r"limited stock|selling out|almost gone)\b", re.I)
_DISCOUNT = re.compile(r"(\d+\s*%\s*off|\bsale\b|\bdiscount\b|\bfree shipping\b|\bfree delivery\b|\bbuy \d+ get\b)", re.I)


def _finding(level: str, where: str, text: str) -> dict:
    return {"level": level, "where": where, "text": text}


def check_text(text: str, where: str, brief: dict | None = None, niche: str = "") -> list[dict]:
    brief = brief or {}
    t = text or ""
    out: list[dict] = []
    if m := _COUNTERFEIT.search(t):
        out.append(_finding("block", where, f'"{m.group(0)}" reads as a counterfeit claim; Meta bans accounts for it. '
                                            "Remove it."))
    if (m := _BRANDS.search(t)) and not _COMPATIBLE.search(t):
        out.append(_finding("block", where, f'Brand name "{m.group(0)}" in the ad: Meta treats it as trademark misuse '
                                            "or a fake. Describe the style instead (for example 'the classic biker look')."))
    if m := _SCREEN.search(t):
        out.append(_finding("warn", where, f'"{m.group(0)}" ties the product to a film, show or person: Meta rejects '
                                           "likeness and trademark use. Say 'inspired by the ... look' without names."))
    elif niche == "celebrity_jackets" and (m := _TITLE_AFTER_FROM.search(t)):
        out.append(_finding("warn", where, f'"{m.group(0)}" may name a film, show or actor: describe the look instead.'))
    if m := _PERSONAL.search(t):
        out.append(_finding("block", where, f'"{m.group(0)}" asserts a personal attribute of the reader, which Meta '
                                            "does not allow. Talk about the product, not the reader's traits."))
    if m := _HEALTH.search(t):
        out.append(_finding("block", where, f'"{m.group(0)}" is a health or body claim Meta rejects.'))
    if m := _INCOME.search(t):
        out.append(_finding("block", where, f'"{m.group(0)}" is an income claim Meta rejects.'))
    if m := _ABSOLUTE.search(t):
        out.append(_finding("warn", where, f'"{m.group(0)}" is an absolute claim; keep it only if you can prove it.'))
    blob = " ".join(str(v) for v in brief.values()).lower()
    if (m := _SCARCITY.search(t)) and not _SCARCITY.search(blob) and "stock" not in blob:
        out.append(_finding("warn", where, f'"{m.group(0)}" is urgency that is not in your brief: false urgency '
                                           "breaks Meta's rules and annoys buyers. Keep it only if it is true."))
    if (m := _DISCOUNT.search(t)) and not (brief.get("offer") or "").strip():
        out.append(_finding("warn", where, f'"{m.group(0)}" promises an offer your brief does not mention: make sure '
                                           "the store really shows it."))
    if re.search(r"[!?]{3,}", t) or len(re.findall(r"\b[A-Z]{4,}\b", t)) > 2:
        out.append(_finding("warn", where, "Shouting (!!! or many words in capitals) lowers ad quality."))
    return out


def check_plan(content: dict, brief: dict | None = None, niche: str = "", product: str = "") -> list[dict]:
    """Every finding in what will reach Meta: the product name (it is the fallback headline and card
    title) and each creative's hook, text and headline. De-duplicated."""
    out, seen = [], set()

    def add(findings):
        for f in findings:
            key = (f["level"], f["where"], f["text"])
            if key not in seen:
                seen.add(key)
                out.append(f)
    if product:
        add(f for f in check_text(product, "Product name", brief, niche) if f["level"] == "block")
    for i, cr in enumerate((content or {}).get("creatives", []) or []):
        if not isinstance(cr, dict):
            continue
        for field in ("hook", "primary_text", "headline"):
            add(check_text(str(cr.get(field) or ""), f"Ad {i + 1} {field.replace('_', ' ')}", brief, niche))
    return out


# Meta ad account statuses (AdAccount.account_status).
ACCOUNT_STATUS = {1: "active", 2: "disabled", 3: "unsettled (a payment is due)", 7: "pending a risk review",
                  8: "pending settlement", 9: "in a grace period", 100: "pending closure", 101: "closed",
                  201: "active", 202: "closed"}
USABLE = {1, 9, 201}


def account_problem(info: dict) -> str:
    """'' when the ad account can take changes; otherwise why it must be left alone."""
    raw = (info or {}).get("account_status")
    if raw is None:
        return ""
    try:
        st = int(raw)
    except (TypeError, ValueError):
        return ""
    if st in USABLE:
        return ""
    return (f"The ad account is {ACCOUNT_STATUS.get(st, f'in status {st}')}. Fix it in Meta's Business Support "
            "Home or billing first; the app does not touch an account in this state.")
