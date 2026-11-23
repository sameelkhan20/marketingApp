"""AI creatives for a plan: build the prompt, call the image provider, store the file.

Prompt = plan creative's visual_prompt + niche guidance + brand style + safety lines.
The product photo (settings.product_photo) is passed as the reference image so the
jacket stays accurate. Files live in data/media and are served at /media/<name>.
"""

import secrets
from pathlib import Path

from sqlalchemy.orm import Session

from . import imagegen, settings_store
from .benchmarks import get_niche
from .config import MEDIA_DIR
from .db import Creative, Plan

ASPECT_FROM_FORMAT = {"9:16": "9:16", "4:5": "4:5", "1:1": "1:1"}
SAFETY = ("Photorealistic advertising photo. No text, no watermarks, no logos, no celebrity likeness, "
          "no copyrighted characters. Keep the product exactly as in the reference photo.")


def aspect_for(fmt: str) -> str:
    for key, aspect in ASPECT_FROM_FORMAT.items():
        if key in (fmt or ""):
            return aspect
    return "1:1"


def build_prompt(plan: Plan, creative: dict, brand_style: str) -> str:
    niche = get_niche(plan.niche)
    parts = [
        creative.get("visual_prompt") or f"Advertising photo of {plan.product}.",
        f"Product: {plan.product}. Audience: {niche['label']} buyers in {plan.country}.",
    ]
    if brand_style:
        parts.append(f"Brand style: {brand_style}.")
    if plan.niche == "celebrity_jackets":
        parts.append("Do not depict any real person, actor or film still; an anonymous model only.")
    parts.append(SAFETY)
    return " ".join(parts)


def generate_for_plan(session: Session, plan: Plan, creative_index: int, aspect: str | None = None) -> Creative:
    content = plan.versions[-1].content
    creatives = content.get("creatives", [])
    if not 0 <= creative_index < len(creatives):
        raise ValueError("creative_index out of range")
    c = creatives[creative_index]
    vals = settings_store.get_all(session)
    aspect = aspect or aspect_for(c.get("format", ""))
    prompt = build_prompt(plan, c, vals.get("brand_style", ""))

    reference = None
    if vals.get("product_photo"):
        p = MEDIA_DIR / vals["product_photo"]
        if p.exists():
            reference = str(p)

    data = imagegen.generate(prompt, reference=reference, aspect=aspect)
    name = f"plan{plan.id}_c{creative_index}_{secrets.token_urlsafe(16)}.png"
    (MEDIA_DIR / name).write_bytes(data)

    row = Creative(plan_id=plan.id, source="ai", provider=imagegen.IMAGE_PROVIDER, creative_index=creative_index,
                   format=aspect, prompt=prompt, file_name=name, selected=1)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def save_upload(session: Session, plan: Plan, creative_index: int, data: bytes, ext: str) -> Creative:
    ext = ext.lower() if ext.lower() in ("png", "jpg", "jpeg", "webp") else "png"
    name = f"plan{plan.id}_u{creative_index}_{secrets.token_urlsafe(16)}.{ext}"
    (MEDIA_DIR / name).write_bytes(data)
    row = Creative(plan_id=plan.id, source="upload", creative_index=creative_index, format="1:1",
                   file_name=name, selected=1)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def delete_file(name: str) -> None:
    p = MEDIA_DIR / name
    if p.exists() and p.resolve().parent == MEDIA_DIR.resolve():
        p.unlink()


def creative_out(c: Creative) -> dict:
    return {"id": c.id, "plan_id": c.plan_id, "created_at": c.created_at.isoformat(), "source": c.source,
            "provider": c.provider, "creative_index": c.creative_index, "format": c.format, "prompt": c.prompt,
            "url": f"/media/{c.file_name}", "selected": bool(c.selected), "meta_image_hash": c.meta_image_hash}


def image_kind(data: bytes) -> str:
    """The real format from the file's first bytes ('' when it is not PNG, JPEG or WEBP)."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return ""
