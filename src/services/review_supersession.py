"""Close project reviews whose email evidence has been superseded."""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from src.db.savepoint import savepoint
from src.email.source_chronology import find_newer_substantive_email
from src.events.bus import Event, event_bus
from src.project_state.observability import project_state_metrics
from src.repositories.activity_log_repo import insert_activity

logger = logging.getLogger(__name__)



@dataclass(frozen=True)
class SupersessionResult:
    """Result of closing a review against superseding email evidence."""

    outcome: str
    message: str
    update_id: str
    project_id: str | None = None


class _SupersessionClaimLost(Exception):
    """The review changed while checking newer email evidence."""


def reject_superseded_email_review(
    conn: sqlite3.Connection,
    *,
    data: dict[str, Any],
    changes: list[dict[str, Any]],
    actor: str,
    surface: str,
    commit: bool = True,
    post_commit: list[Callable[[], None]] | None = None,
) -> SupersessionResult | None:
    """Reject a pending review when a newer substantive email supersedes it."""
    superseding_email = _find_superseding_email(conn, data, changes)
    if superseding_email is None:
        return None
    return _reject_superseded_review(
        conn,
        data=data,
        superseding_email=superseding_email,
        actor=actor,
        surface=surface,
        commit=commit,
        post_commit=post_commit,
    )


def _find_superseding_email(
    conn: sqlite3.Connection,
    data: dict[str, Any],
    changes: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Return newer substantive evidence from the same email thread, if any."""
    project_id = data["project_id"]
    source = _resolve_review_source_email(conn, project_id, data, changes)
    if source is None:
        return None
    return find_newer_substantive_email(
        conn,
        project_id=project_id,
        source_email_id=source["email_id"],
    )


def _resolve_review_source_email(
    conn: sqlite3.Connection,
    project_id: str,
    data: dict[str, Any],
    changes: list[dict[str, Any]],
) -> sqlite3.Row | None:
    refs: list[str] = []
    if data.get("source_ref"):
        refs.append(str(data["source_ref"]))
    refs.extend(
        str(change["evidence_ref"])
        for change in changes
        if change.get("evidence_ref")
    )

    seen: set[str] = set()
    for raw_ref in refs:
        ref = raw_ref.strip()
        if ref.lower().startswith("email:"):
            ref = ref.split(":", 1)[1].strip()
        if not ref or ref in seen:
            continue
        seen.add(ref)
        row = _load_processed_email(conn, project_id, ref)
        if row is not None:
            return row
        evidence = conn.execute(
            """SELECT source_ref FROM evidence_store
               WHERE evidence_id=? AND project_id=? AND evidence_type='email'""",
            (ref, project_id),
        ).fetchone()
        if evidence and evidence[0]:
            row = _load_processed_email(conn, project_id, str(evidence[0]))
            if row is not None:
                return row
    return None


def _load_processed_email(
    conn: sqlite3.Connection,
    project_id: str,
    email_id: str,
) -> sqlite3.Row | None:
    columns = _processed_email_columns(conn)
    received_expr = "received_at" if "received_at" in columns else "NULL"
    return conn.execute(
        f"""SELECT email_id, subject, thread_id, thread_fp,
                   COALESCE({received_expr}, processed_at) AS evidence_at
            FROM processed_emails
            WHERE project_id=? AND email_id=?""",
        (project_id, email_id),
    ).fetchone()


def _processed_email_columns(conn: sqlite3.Connection) -> set[str]:
    return {row[1] for row in conn.execute("PRAGMA table_info(processed_emails)")}


def _reject_superseded_review(
    conn: sqlite3.Connection,
    *,
    data: dict[str, Any],
    superseding_email: dict[str, Any],
    actor: str,
    surface: str,
    commit: bool,
    post_commit: list[Callable[[], None]] | None,
) -> SupersessionResult:
    if not commit and post_commit is None:
        raise ValueError("post_commit queue is required for caller-owned review writes")
    update_id = data["update_id"]
    project_id = data["project_id"]
    newer_email_id = superseding_email["email_id"]
    evidence_at = superseding_email.get("evidence_at") or "an unknown time"
    reason = (
        f"Review evidence {superseding_email['source_email_id']} was superseded by "
        f"newer email {newer_email_id} at {evidence_at}. Reassess the latest thread "
        "before approving."
    )
    rejection_payload = json.dumps(
        {"tags": ["superseded_evidence"], "reason": reason},
        ensure_ascii=False,
    )
    now = datetime.now(timezone.utc).isoformat()
    try:
        with savepoint(conn, ensure_transaction=True):
            claimed = conn.execute(
                """UPDATE proposed_updates
                   SET decision_outcome='rejected', rejection_reasons=?, decided_at=?
                   WHERE update_id=? AND decision_outcome='needs_review'""",
                (rejection_payload, now, update_id),
            )
            if claimed.rowcount != 1:
                raise _SupersessionClaimLost()

            metadata = json.dumps(
                {
                    "source_email_id": superseding_email["source_email_id"],
                    "superseding_email_id": newer_email_id,
                    "evidence_at": evidence_at,
                },
                ensure_ascii=False,
            )
            insert_activity(
                conn,
                actor=actor,
                action_type="review_superseded",
                project_id=project_id,
                surface=surface,
                object_ref=update_id,
                summary=reason,
                metadata_json=metadata,
            )
    except _SupersessionClaimLost:
        return SupersessionResult(
            outcome="not_found",
            message="Review not found or already processed",
            update_id=update_id,
        )
    def after_commit() -> None:
        try:
            event_bus.publish(Event(
                event_type="review_superseded",
                project_id=project_id,
                detail={
                    "update_id": update_id,
                    "source_email_id": superseding_email["source_email_id"],
                    "superseding_email_id": newer_email_id,
                    "evidence_at": evidence_at,
                },
                surface=surface,
            ))
        except Exception:
            logger.exception("Committed review supersession event publication failed")
        project_state_metrics.increment("stale_proposal_rejection")
        project_state_metrics.record_review("rejected")

    if commit:
        conn.commit()
        try:
            after_commit()
        except Exception:
            logger.exception("Committed review supersession post-commit effect failed")
    else:
        assert post_commit is not None
        post_commit.append(after_commit)
    return SupersessionResult(
        outcome="superseded",
        message=reason,
        update_id=update_id,
        project_id=project_id,
    )
