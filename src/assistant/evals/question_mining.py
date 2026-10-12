"""Email-driven PM question mining and human review workflow."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Sequence

from src.assistant.workspace_read_support import normalize_timestamp

from .artifacts import content_hash, redact_value, stamp_content_hash, verify_content_hash
from .models import (
    ApprovedQuestionBatch,
    ArtifactLifecycle,
    EmailMiningSignal,
    EvalCase,
    QuestionMiningBatch,
    QuestionReviewBatch,
    QuestionReviewDecision,
    QuestionReviewItem,
    QuestionReviewStamp,
)

_SUBJECT_PREFIX_RE = re.compile(r"^\s*(?:(?:re|fw|fwd)\s*:\s*)+", re.IGNORECASE)
_NON_WORD_RE = re.compile(r"[^a-z0-9]+")


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _safe_json(value: Any, fallback: Any) -> Any:
    if value in (None, ""):
        return fallback
    try:
        parsed = json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return fallback
    return parsed


def _aware_timestamp(value: Any) -> datetime | None:
    normalized = normalize_timestamp(value)
    if not normalized:
        return None
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _email_ref(email_id: Any) -> str:
    digest = hashlib.sha256(str(email_id or "").encode("utf-8")).hexdigest()[:16]
    return f"email:{digest}"


def _thread_key(row: dict[str, Any]) -> str:
    for field in ("thread_fp", "thread_id"):
        value = str(row.get(field) or "").strip()
        if value:
            return f"{field}:{hashlib.sha1(value.encode('utf-8')).hexdigest()[:16]}"
    subject = _SUBJECT_PREFIX_RE.sub("", str(row.get("subject") or "")).strip().casefold()
    subject = " ".join(subject.split())
    if subject:
        return f"subject:{hashlib.sha1(subject.encode('utf-8')).hexdigest()[:16]}"
    return _email_ref(row.get("email_id"))


def _signal_id(kind: str, key: str) -> str:
    digest = hashlib.sha1(f"{kind}:{key}".encode("utf-8")).hexdigest()[:14]
    return f"{kind}-{digest}"


def _project_snapshots(conn: sqlite3.Connection, limit: int) -> dict[str, dict[str, Any]]:
    columns = _columns(conn, "projects")
    preferred = [
        "project_id",
        "client_name",
        "market",
        "project_status",
        "current_milestone",
        "ball_with",
        "target_go_live",
        "next_action",
        "last_activity_at",
        "updated_at",
    ]
    selected = [column for column in preferred if column in columns]
    if "project_id" not in selected:
        return {}
    projection = ", ".join(f'"{column}"' for column in selected)
    rows = conn.execute(
        f"SELECT {projection} FROM projects "
        "WHERE project_id != '_system' ORDER BY project_id LIMIT ?",
        (limit,),
    ).fetchall()
    return {str(row["project_id"]): dict(row) for row in rows}


def _balanced_email_rows(
    conn: sqlite3.Connection,
    *,
    as_of: datetime,
    project_ids: list[str],
    messages_per_project: int,
    email_limit: int,
) -> list[dict[str, Any]]:
    columns = _columns(conn, "processed_emails")
    required = {"email_id", "project_id"}
    if not required.issubset(columns) or not project_ids:
        return []
    preferred = [
        "email_id",
        "project_id",
        "processed_at",
        "received_at",
        "thread_fp",
        "thread_id",
        "in_reply_to",
        "references_chain",
        "subject",
        "direction",
        "body_snippet",
        "result",
    ]
    selected = [column for column in preferred if column in columns]
    projection = ", ".join(f'"{column}"' for column in selected)
    timestamp_expr = (
        "COALESCE(received_at, processed_at)"
        if {"received_at", "processed_at"}.issubset(columns)
        else ("received_at" if "received_at" in columns else "processed_at")
    )
    placeholders = ",".join("?" for _ in project_ids)
    rows = conn.execute(
        f"""WITH ranked AS (
                SELECT {projection},
                       ROW_NUMBER() OVER (
                           PARTITION BY project_id
                           ORDER BY datetime({timestamp_expr}) DESC, email_id DESC
                       ) AS project_rank
                FROM processed_emails
                WHERE project_id IN ({placeholders})
                  AND datetime({timestamp_expr}) <= datetime(?)
            )
            SELECT * FROM ranked
            WHERE project_rank <= ?
            ORDER BY datetime({timestamp_expr}) DESC, email_id DESC
            LIMIT ?""",
        [*project_ids, as_of.isoformat(), messages_per_project, email_limit],
    ).fetchall()
    return [dict(row) for row in rows]


def _party_email_signals(
    conn: sqlite3.Connection,
    *,
    as_of: datetime,
    project_snapshots: dict[str, dict[str, Any]],
    limit: int,
) -> list[EmailMiningSignal]:
    required_tables = {"project_parties", "organisations", "processed_emails"}
    if not all(_table_exists(conn, table) for table in required_tables):
        return []
    parties = conn.execute(
        """SELECT o.org_id, o.org_name, o.org_type, pp.party_role,
                  GROUP_CONCAT(DISTINCT pp.project_id) AS project_ids
           FROM project_parties pp
           JOIN organisations o ON o.org_id = pp.org_id
           WHERE pp.project_id != '_system'
           GROUP BY o.org_id, o.org_name, o.org_type, pp.party_role
           ORDER BY CASE pp.party_role WHEN 'vendor' THEN 0 ELSE 1 END, o.org_name
           LIMIT ?""",
        (limit,),
    ).fetchall()
    signals: list[EmailMiningSignal] = []
    for party in parties:
        party_name = str(party["org_name"] or "").strip()
        if len(party_name) < 3:
            continue
        project_ids = [
            item for item in str(party["project_ids"] or "").split(",")
            if item in project_snapshots
        ]
        if not project_ids:
            continue
        placeholders = ",".join("?" for _ in project_ids)
        rows = conn.execute(
            f"""SELECT email_id, project_id, received_at, processed_at, subject,
                       direction, body_snippet
                FROM processed_emails
                WHERE project_id IN ({placeholders})
                  AND datetime(COALESCE(received_at, processed_at)) <= datetime(?)
                  AND LOWER(COALESCE(subject, '') || ' ' || COALESCE(body_snippet, ''))
                      LIKE LOWER(?)
                ORDER BY datetime(COALESCE(received_at, processed_at)) DESC
                LIMIT 4""",
            [*project_ids, as_of.isoformat(), f"%{party_name}%"],
        ).fetchall()
        if not rows:
            continue
        latest = dict(rows[0])
        snippets = [str(row["body_snippet"] or "").strip() for row in rows]
        payload, _ = redact_value({
            "subject": latest.get("subject") or party_name,
            "summary": "\n".join(item for item in snippets if item)[:1600],
            "party_name": party_name,
            "party_role": party["party_role"],
            "organisation_type": party["org_type"],
        })
        signals.append(EmailMiningSignal(
            signal_id=_signal_id("party-email", str(party["org_id"])),
            kind="portfolio_email_pattern",
            project_ids=project_ids,
            email_refs=[_email_ref(row["email_id"]) for row in rows],
            occurred_at=_aware_timestamp(latest.get("received_at") or latest.get("processed_at")),
            latest_direction=str(latest.get("direction") or "").casefold() or None,
            subject=str(payload["subject"]),
            summary=str(payload["summary"]),
            project_snapshot={
                project_id: project_snapshots[project_id]
                for project_id in project_ids
            },
            metadata={
                "party_name": payload["party_name"],
                "party_role": payload["party_role"],
                "organisation_type": payload["organisation_type"],
                "email_match_count": len(rows),
            },
        ))
    return signals


def build_email_mining_signals(
    conn: sqlite3.Connection,
    *,
    as_of: datetime,
    project_limit: int = 100,
    messages_per_project: int = 8,
    email_limit: int = 400,
    signal_limit: int = 120,
    party_signal_limit: int = 30,
    exclude_project_ids: Sequence[str] = (),
    exclude_signal_ids: Sequence[str] = (),
) -> list[EmailMiningSignal]:
    """Build a bounded, redacted email corpus balanced across visible projects."""
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    excluded_projects = {item.strip() for item in exclude_project_ids if item.strip()}
    excluded_signals = {item.strip() for item in exclude_signal_ids if item.strip()}
    snapshots = _project_snapshots(conn, project_limit)
    snapshots = {
        project_id: snapshot
        for project_id, snapshot in snapshots.items()
        if project_id not in excluded_projects
    }
    rows = _balanced_email_rows(
        conn,
        as_of=as_of,
        project_ids=sorted(snapshots),
        messages_per_project=messages_per_project,
        email_limit=email_limit,
    )
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        project_id = str(row.get("project_id") or "").strip()
        if project_id:
            grouped[(project_id, _thread_key(row))].append(row)

    signals: list[EmailMiningSignal] = []
    for (project_id, thread_key), messages in grouped.items():
        messages.sort(
            key=lambda item: normalize_timestamp(item.get("received_at") or item.get("processed_at")),
            reverse=True,
        )
        latest = messages[0]
        summaries = [str(item.get("body_snippet") or "").strip() for item in messages[:3]]
        payload, _ = redact_value({
            "subject": latest.get("subject") or "Email thread",
            "summary": "\n".join(item for item in summaries if item)[:1600],
            "project_snapshot": snapshots.get(project_id, {}),
        })
        signals.append(EmailMiningSignal(
            signal_id=_signal_id("email-thread", f"{project_id}:{thread_key}"),
            project_id=project_id,
            project_ids=[project_id],
            email_refs=[_email_ref(item.get("email_id")) for item in messages[:5]],
            thread_key=thread_key,
            occurred_at=_aware_timestamp(latest.get("received_at") or latest.get("processed_at")),
            latest_direction=str(latest.get("direction") or "").casefold() or None,
            subject=str(payload["subject"]),
            summary=str(payload["summary"]),
            project_snapshot=dict(payload["project_snapshot"]),
            metadata={
                "message_count_in_sample": len(messages),
                "directions": sorted({
                    str(item.get("direction") or "").casefold()
                    for item in messages
                    if item.get("direction")
                }),
            },
        ))

    signals.sort(
        key=lambda item: (
            item.occurred_at or datetime.min.replace(tzinfo=timezone.utc),
            item.signal_id,
        ),
        reverse=True,
    )
    party_signals = _party_email_signals(
        conn,
        as_of=as_of,
        project_snapshots=snapshots,
        limit=party_signal_limit,
    )
    combined = [*party_signals, *signals]
    deduped: list[EmailMiningSignal] = []
    seen: set[str] = set()
    for signal in combined:
        if signal.signal_id in seen or signal.signal_id in excluded_signals:
            continue
        seen.add(signal.signal_id)
        deduped.append(signal)
        if len(deduped) >= signal_limit:
            break
    return deduped


def fixture_fingerprint(conn: sqlite3.Connection, *, as_of: datetime) -> str:
    """Return a stable fixture watermark without hashing or exposing the DB file."""
    payload: dict[str, Any] = {"as_of": as_of.isoformat()}
    for table in ("projects", "processed_emails", "project_parties", "organisations"):
        if _table_exists(conn, table):
            payload[f"{table}_count"] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    if _table_exists(conn, "processed_emails"):
        payload["email_watermark"] = conn.execute(
            "SELECT MAX(COALESCE(received_at, processed_at)) FROM processed_emails"
        ).fetchone()[0]
    return content_hash(payload)


def make_review_template(batch: QuestionMiningBatch) -> QuestionReviewBatch:
    if not verify_content_hash(batch):
        raise ValueError("question mining batch content_hash is missing or invalid")
    return QuestionReviewBatch(
        batch_id=batch.batch_id,
        source_content_hash=batch.content_hash,
        decisions=[QuestionReviewItem(case_id=case.case_id) for case in batch.cases],
    )


def approve_question_batch(
    batch: QuestionMiningBatch,
    review: QuestionReviewBatch,
    *,
    reviewed_by: str | None = None,
    review_notes: str | None = None,
    reviewed_at: datetime | None = None,
    fixture_db_path: str | None = None,
) -> tuple[ApprovedQuestionBatch, QuestionReviewBatch]:
    """Validate explicit human decisions and freeze the approved question wording."""
    if not verify_content_hash(batch):
        raise ValueError("question mining batch content_hash is missing or invalid")
    if review.batch_id != batch.batch_id or review.source_content_hash != batch.content_hash:
        raise ValueError("question review does not match the mined question batch")
    reviewer = str(reviewed_by or review.reviewed_by).strip()
    if not reviewer:
        raise ValueError("reviewed_by is required")
    notes = str(review_notes if review_notes is not None else review.review_notes).strip()
    effective_reviewed_at = reviewed_at or review.reviewed_at or datetime.now(timezone.utc)
    if effective_reviewed_at.tzinfo is None or effective_reviewed_at.utcoffset() is None:
        raise ValueError("reviewed_at must be timezone-aware")

    case_by_id = {case.case_id: case for case in batch.cases}
    decision_by_id: dict[str, QuestionReviewItem] = {}
    for decision in review.decisions:
        if decision.case_id not in case_by_id:
            raise ValueError(f"review references unknown case_id: {decision.case_id}")
        if decision.case_id in decision_by_id:
            raise ValueError(f"duplicate review decision for {decision.case_id}")
        decision_by_id[decision.case_id] = decision
    missing = sorted(set(case_by_id) - set(decision_by_id))
    if missing:
        raise ValueError(f"question review is missing decisions for: {', '.join(missing)}")
    pending = sorted(
        case_id for case_id, decision in decision_by_id.items()
        if decision.decision == QuestionReviewDecision.PENDING
    )
    if pending:
        raise ValueError(f"question review still has pending decisions: {', '.join(pending)}")

    approved: list[EvalCase] = []
    rejected: list[str] = []
    for original in batch.cases:
        decision = decision_by_id[original.case_id]
        if decision.decision == QuestionReviewDecision.REJECT:
            rejected.append(original.case_id)
            continue
        question = original.question
        original_question = None
        decision_value = "approve"
        if decision.decision == QuestionReviewDecision.EDIT:
            question = str(decision.edited_question or "").strip()
            original_question = original.question
            decision_value = "edit"
        approved.append(original.model_copy(update={
            "lifecycle": ArtifactLifecycle.QUESTION_APPROVED,
            "question": question,
            "question_review": QuestionReviewStamp(
                reviewed_by=reviewer,
                reviewed_at=effective_reviewed_at,
                decision=decision_value,
                review_notes=decision.notes,
                original_question=original_question,
            ),
            "content_hash": "",
        }))
    if not approved:
        raise ValueError("question review approved no cases")

    final_review = stamp_content_hash(review.model_copy(update={
        "reviewed_by": reviewer,
        "reviewed_at": effective_reviewed_at,
        "review_notes": notes,
        "content_hash": "",
    }))
    approved_batch = stamp_content_hash(ApprovedQuestionBatch(
        batch_id=batch.batch_id,
        source_content_hash=batch.content_hash,
        review_content_hash=final_review.content_hash,
        reviewed_by=reviewer,
        reviewed_at=effective_reviewed_at,
        review_notes=notes,
        fixture_db_path=fixture_db_path,
        cases=approved,
        rejected_case_ids=rejected,
    ))
    return approved_batch, final_review


def render_question_review_markdown(batch: QuestionMiningBatch) -> str:
    """Render mined questions and their email provenance for human review."""
    signal_by_id = {signal.signal_id: signal for signal in batch.signals}
    lines = [
        "# AI Workspace PM Question Review",
        "",
        f"- Batch: `{batch.batch_id}`",
        f"- Frozen as-of: `{batch.as_of.isoformat()}`",
        f"- Mined questions: **{len(batch.cases)}**",
        f"- Email-derived signals: **{len(batch.signals)}**",
        f"- Holdout against suites: {', '.join(f'`{item}`' for item in batch.holdout_against_suite_ids) or 'none'}",
        f"- Excluded prior cases: **{len(batch.excluded_case_ids)}**",
        f"- Excluded prior projects: **{len(batch.excluded_project_ids)}**",
        f"- Excluded prior signals: **{len(batch.excluded_signal_ids)}**",
        "",
        "Edit `question_review.json`: set every decision to `approve`, `edit`, or `reject`. ",
        "For `edit`, also provide `edited_question`. Golden answers cannot be built while any decision is pending.",
    ]
    for index, case in enumerate(batch.cases, start=1):
        lines.extend([
            "",
            "---",
            "",
            f"## {index}. `{case.case_id}`",
            "",
            f"> {case.question}",
            "",
            f"- PM intent: `{case.pm_intent or 'not supplied'}`",
            f"- PM rationale: {case.pm_rationale or 'Not supplied.'}",
            f"- Scope: `{case.scope_mode}`",
            f"- Projects: {', '.join(f'`{item}`' for item in case.project_ids) or 'portfolio'}",
            f"- Expected evidence tool: `{case.expected_tool}`",
            f"- Tool arguments: `{json.dumps(case.evidence_tool_args, ensure_ascii=False, sort_keys=True)}`",
            f"- Proposed decision: `pending`",
            "",
            "### Email-derived signals",
        ])
        for signal_id in case.source_signal_ids:
            signal = signal_by_id.get(signal_id)
            if signal is None:
                lines.append(f"- `{signal_id}` ? missing from batch")
                continue
            project_label = signal.project_id or ", ".join(signal.project_ids) or "portfolio"
            time_label = signal.occurred_at.isoformat() if signal.occurred_at else "unknown time"
            lines.extend([
                f"- `{signal.signal_id}` - project `{project_label}`, {time_label}, latest `{signal.latest_direction or 'unknown'}`",
                f"  - Subject: {signal.subject or 'No subject'}",
                f"  - Signal: {(signal.summary or 'No snippet available')[:500]}",
            ])
    return "\n".join(lines).rstrip() + "\n"


__all__ = [
    "approve_question_batch",
    "build_email_mining_signals",
    "fixture_fingerprint",
    "make_review_template",
    "render_question_review_markdown",
]
