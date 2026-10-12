"""Format digest queue items into a single Telegram notification."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

from src.repositories.digest_queue_repo import get_pending_items, mark_delivered
from src.telegram.i18n import t, get_ui_language


SECTION_ORDER = (
    "sla_alert",
    "followup_overdue",
    "ticket_aging",
    "support_ticket",
    "email_ambiguous",
    "email_deferred",
    "system_maintenance",
)

SECTION_LABELS = {
    "sla_alert": "sched.digest_section_sla",
    "followup_overdue": "sched.digest_section_followup",
    "ticket_aging": "sched.digest_section_ticket_aging",
    "support_ticket": "sched.digest_section_support",
    "email_ambiguous": "sched.digest_section_email_ambiguous",
    "email_deferred": "sched.digest_section_email_deferred",
}

MAX_ITEMS_PER_SECTION = 5


@dataclass
class DigestSection:
    category: str
    label: str
    items: list[dict] = field(default_factory=list)
    total_count: int = 0


@dataclass
class DigestMessage:
    generated_at: str
    total_items: int
    sections: list[DigestSection] = field(default_factory=list)
    informational_count: int = 0


def generate_digest(conn: sqlite3.Connection) -> DigestMessage | None:
    """Build a digest from all pending items. Returns None if queue is empty."""
    items = get_pending_items(conn)
    if not items:
        return None

    grouped: dict[str, list[dict]] = {}
    informational = 0
    seen = set()
    for item in items:
        if (item.get("project_id") or "").startswith("_"):
            continue
        try:
            meta = json.loads(item.get("metadata_json") or "{}")
        except (ValueError, TypeError):
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        informational_only = (item["category"] == "email_information" or meta.get("action_required") is False
            or (item["category"] == "email_deferred" and (item.get("body") or "").strip().lower() == "no action required"))
        if informational_only:
            informational += 1
            continue
        if meta.get("notification_kind") == "system_maintenance":
            item = dict(item, category="system_maintenance")
        source_id = (meta.get("followup_id") or meta.get("ticket_id") or meta.get("email_id")
                     or meta.get("insight_id") or meta.get("related_id"))
        # Equal text is not proof that two tasks are the same. Coalesce repeated
        # notifications only when they share an actual work-item/source identity.
        signature = (item["category"], item.get("project_id"), source_id or item["id"], item["title"], item.get("body"))
        if signature in seen:
            continue
        seen.add(signature)
        grouped.setdefault(item["category"], []).append(item)

    sections = []
    for cat in (*SECTION_ORDER, *(cat for cat in grouped if cat not in SECTION_ORDER)):
        if cat not in grouped:
            continue
        cat_items = grouped[cat]
        label_key = SECTION_LABELS.get(cat, cat)
        section = DigestSection(
            category=cat,
            label=("系統維護（非緊急）" if cat == "system_maintenance" and get_ui_language() != "en"
                   else "System maintenance (non-urgent)" if cat == "system_maintenance" else t(label_key)),
            items=cat_items[:MAX_ITEMS_PER_SECTION],
            total_count=len(cat_items),
        )
        sections.append(section)

    hkt = timezone(timedelta(hours=8))
    now_hkt = datetime.now(hkt)

    return DigestMessage(
        generated_at=now_hkt.strftime("%H:%M"),
        total_items=sum(section.total_count for section in sections),
        sections=sections,
        informational_count=informational,
    )


def format_digest_text(digest: DigestMessage) -> str:
    """Render the digest into Telegram-compatible text (locale from /lang)."""
    lines = [
        t("sched.digest_header").format(time=digest.generated_at),
        "━" * 23,
    ]

    for section in digest.sections:
        lines.append(f"\n{section.label} ({section.total_count})")
        for item in section.items:
            title = item["title"]
            body_first_line = " | ".join((item["body"] or "").splitlines()[:4])[:350]
            if body_first_line:
                lines.append(f"  • {title} — {body_first_line}")
            else:
                lines.append(f"  • {title}")
        overflow = section.total_count - len(section.items)
        if overflow > 0:
            lines.append(t("sched.digest_overflow").format(count=overflow))

    if not digest.total_items:
        lines.append("No action items in this digest." if get_ui_language() == "en" else "本次摘要沒有需要處理的項目。")
    if digest.informational_count:
        lines.append((f"\nFor information only: {digest.informational_count} email(s); not counted as pending actions."
                      if get_ui_language() == "en" else f"\n僅供參考：{digest.informational_count}封郵件，未列作待辦；需要時可查看收件記錄。"))
    return "\n".join(lines)


def flush_and_format(conn: sqlite3.Connection) -> tuple[str | None, int]:
    """Atomic: generate, format, and mark all items delivered.

    Returns (formatted_text, item_count). text is None if no items.
    """
    items = get_pending_items(conn)
    if not items:
        return None, 0

    digest = generate_digest(conn)
    if not digest:
        return None, 0

    text = format_digest_text(digest)
    item_ids = [item["id"] for item in items]
    batch_id = str(uuid.uuid4())[:8]
    mark_delivered(conn, item_ids, batch_id)

    return text, digest.total_items
