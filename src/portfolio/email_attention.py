"""Deterministic external-email attention assessment for portfolio surfaces."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

_NON_ACTIONABLE_INSIGHT_CATEGORIES = frozenset({
    "newsletter", "system_notification", "fyi", "spam",
})
_EMAIL_ACTION_CONFIDENCE = 0.75
# Unscored inbound threads older than this stay out of the daily brief.
_REVIEW_MAX_AGE_DAYS = 14


@dataclass(frozen=True)
class EmailAttentionCandidate:
    """External inbound conversation that may require operator attention."""

    email_id: str
    thread_key: str
    processed_at: str
    sender_email: str | None
    subject: str
    confidence_tier: str  # likely | review
    assessment_reason: str


@dataclass(frozen=True)
class EmailAttentionAssessment:
    """Deterministic assessment of external email conversations."""

    likely: tuple[EmailAttentionCandidate, ...] = ()
    review: tuple[EmailAttentionCandidate, ...] = ()
    has_old_likely: bool = False


def normalize_thread_key(value: str | None) -> str | None:
    """Normalize historical Message-ID variants for conversation grouping."""
    normalized = (value or "").strip()
    while len(normalized) >= 2 and normalized.startswith("<") and normalized.endswith(">"):
        normalized = normalized[1:-1].strip()
    return normalized.lower() or None


def assess_email_attention(
    conn: sqlite3.Connection, project_id: str, now: datetime
) -> EmailAttentionAssessment:
    """Assess external inbound conversations without claiming every one needs a reply."""
    from config.settings import CONFIG

    internal_domains = {
        domain.lower() for domain in getattr(CONFIG, "company_domains", set())
    }
    try:
        rows = conn.execute(
            """SELECT pe.email_id, pe.thread_fp, pe.thread_id, pe.processed_at,
                      pe.sender_email, pe.subject, pe.direction, pe.result,
                      pe.ticket_id,
                      ii.action_required, ii.suppressed, ii.confidence, ii.category
               FROM processed_emails pe
               LEFT JOIN inbox_insights ii ON ii.email_id = pe.email_id
               WHERE pe.project_id = ?
                 AND COALESCE(pe.thread_fp, pe.thread_id, pe.email_id) IS NOT NULL
               ORDER BY pe.processed_at DESC, pe.email_id DESC""",
            (project_id,),
        ).fetchall()
        has_insight_columns = True
    except sqlite3.OperationalError:
        rows = conn.execute(
            """SELECT email_id, thread_fp, thread_id, processed_at,
                      sender_email, subject, direction, result, ticket_id
               FROM processed_emails
               WHERE project_id = ?
                 AND COALESCE(thread_fp, thread_id, email_id) IS NOT NULL
               ORDER BY processed_at DESC, email_id DESC""",
            (project_id,),
        ).fetchall()
        has_insight_columns = False

    latest_by_thread: dict[str, sqlite3.Row] = {}
    for row in rows:
        thread_key = normalize_thread_key(
            row["thread_fp"] or row["thread_id"] or row["email_id"]
        )
        if thread_key and thread_key not in latest_by_thread:
            latest_by_thread[thread_key] = row

    likely: list[EmailAttentionCandidate] = []
    review: list[EmailAttentionCandidate] = []
    threshold = (now - timedelta(hours=24)).isoformat()

    for thread_key, row in latest_by_thread.items():
        if row["direction"] != "inbound":
            continue
        sender = row["sender_email"]
        if not _is_external_email(sender, internal_domains):
            continue
        if (row["result"] or "").lower() == "operational":
            continue
        if row["ticket_id"]:
            continue

        action_required = row["action_required"] if has_insight_columns else None
        suppressed = row["suppressed"] if has_insight_columns else None
        confidence = row["confidence"] if has_insight_columns else None
        category = (row["category"] or "").lower() if has_insight_columns else ""
        has_insight = any(
            value is not None for value in (action_required, suppressed, confidence)
        ) or bool(category)

        if has_insight and (
            bool(suppressed)
            or action_required == 0
            or category in _NON_ACTIONABLE_INSIGHT_CATEGORIES
        ):
            continue

        tier = "review"
        reason = "No persisted actionability assessment"
        if (
            has_insight
            and action_required == 1
            and not bool(suppressed)
            and float(confidence or 0.0) >= _EMAIL_ACTION_CONFIDENCE
        ):
            tier = "likely"
            reason = "Inbox intelligence marked action required"
        elif has_insight:
            reason = "Actionability confidence below threshold"

        candidate = EmailAttentionCandidate(
            email_id=row["email_id"],
            thread_key=thread_key,
            processed_at=row["processed_at"],
            sender_email=sender,
            subject=(row["subject"] or "(no subject)").strip(),
            confidence_tier=tier,
            assessment_reason=reason,
        )
        if tier == "likely":
            likely.append(candidate)
            continue
        if _is_stale_review(candidate.processed_at, now):
            continue
        review.append(candidate)

    return EmailAttentionAssessment(
        likely=tuple(likely),
        review=tuple(review),
        has_old_likely=any(item.processed_at < threshold for item in likely),
    )


def format_email_evidence(assessment: EmailAttentionAssessment) -> list[str]:
    """Return concise subject/date evidence without exposing email bodies."""
    candidates = list(assessment.likely or assessment.review)
    evidence = []
    for candidate in candidates[:2]:
        try:
            date_label = datetime.fromisoformat(
                candidate.processed_at.replace("Z", "+00:00")
            ).strftime("%d %b")
        except (TypeError, ValueError):
            date_label = candidate.processed_at[:10]
        subject = re.sub(r"\s+", " ", candidate.subject).strip()
        if len(subject) > 80:
            subject = subject[:77].rstrip() + "..."
        evidence.append(f"{date_label} — {subject}")
    return evidence


def _is_stale_review(processed_at: str | None, now: datetime) -> bool:
    if not processed_at:
        return True
    try:
        parsed = datetime.fromisoformat(processed_at.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=now.tzinfo)
    return (now - parsed).days >= _REVIEW_MAX_AGE_DAYS


def _is_external_email(sender: str | None, internal_domains: set[str]) -> bool:
    if not sender or not internal_domains:
        return True
    address = sender.lower().strip()
    return not any(address.endswith("@" + domain) for domain in internal_domains)
