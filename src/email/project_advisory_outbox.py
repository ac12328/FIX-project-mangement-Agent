"""Committed, recipient-bound project advisory delivery for inbound updates.

Analysis runs after the route commits. Only a claimed, committed row may cross
Telegram I/O; an unknown send is quarantined rather than automatically replayed.
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from uuid import uuid4

from src.models.advisory import Advisory
from src.email.advisory_delivery_policy import (
    PreparedProjectAdvisory, advisory_semantic_key, claim_prepared_advisory,
    read_advisory_context,
)

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _destination() -> tuple[int | None, str | None]:
    from config.settings import CONFIG

    telegram = CONFIG.telegram
    allowed = list(telegram.allowed_chat_ids or [])
    chat_id = telegram.chat_id or (allowed[0] if allowed else None)
    token = str(telegram.bot_token or "")
    return (int(chat_id) if chat_id else None,
            hashlib.sha256(token.encode()).hexdigest() if token else None)


def enqueue_project_advisory(
    conn: sqlite3.Connection, *, email_id: str, project_id: str,
    subject: str, body: str,
) -> bool:
    """Record the opportunity with its audit; never send under the writer lease."""
    if not email_id or not project_id:
        raise ValueError("invalid advisory identity")
    from config.defaults import EMAIL_BODY_SNIPPET_CHARS

    snippet = body[:EMAIL_BODY_SNIPPET_CHARS]
    subject = subject[:200]
    prior = conn.execute(
        "SELECT project_id,subject,event_summary FROM project_advisory_outbox WHERE email_id=?",
        (email_id,),
    ).fetchone()
    if prior:
        if tuple(prior) != (project_id, subject, snippet):
            raise ValueError("project advisory identity collision")
        return False
    chat_id, fingerprint = _destination()
    bound = bool(chat_id and fingerprint)
    now = _now().isoformat()
    conn.execute("""INSERT INTO project_advisory_outbox
        (email_id,project_id,subject,event_summary,chat_id,bot_fingerprint,
         state,available_at,last_error,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (
            email_id, project_id, subject, snippet,
            chat_id if bound else None, fingerprint if bound else None,
            "pending" if bound else "blocked", now,
            None if bound else "recipient_unbound", now, now,
        ))
    return True


@contextmanager
def _write_scope(conn: sqlite3.Connection | None):
    if conn is None:
        from src.db.connection import scoped_write
        with scoped_write() as owned:
            yield owned
    else:
        if conn.in_transaction:
            raise ValueError("advisory dispatch requires a committed caller transaction")
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


def analyze_project_advisory(project_id: str, subject: str, body: str) -> Advisory:
    from src.advisory.engine import AdvisoryEngine
    from src.db.connection import scoped_read
    from src.email.inbound_policy import _get_project_milestone_and_ball_with

    keywords = [w.lower() for w in subject.split()[:15] if len(w) > 2]
    with scoped_read() as read_conn:
        milestone, ball_with = _get_project_milestone_and_ball_with(read_conn, project_id)
        return AdvisoryEngine().advise_project(
            read_conn, project_id=project_id, current_milestone=milestone,
            ball_with=ball_with, event_keywords=keywords, event_summary=body,
            trusted_internal=True,
        )


def _send(advisory: Advisory, project_id: str, subject: str,
          chat_id: int, fingerprint: str, *,
          source_email_id: str | None = None) -> dict[str, Any]:
    from src.telegram.advisory_push import push_project_advisory_card_sync

    if source_email_id is not None:
        from src.db.connection import scoped_read
        with scoped_read() as db:
            row = db.execute("SELECT snapshot_json,project_id,chat_id,bot_fingerprint,state FROM project_advisory_outbox WHERE email_id=?",
                             (source_email_id,)).fetchone()
            current = read_advisory_context(db, project_id)
            if (row is None or row["state"] != "sending"
                    or (row["project_id"], row["chat_id"], row["bot_fingerprint"]) != (project_id, chat_id, fingerprint)
                    or current.pending_review_ids or row["snapshot_json"] != current.snapshot_json):
                return {"sent": False, "error": "project_state_changed"}
    return push_project_advisory_card_sync(
        None, advisory=advisory, project_id=project_id,
        event_summary=subject, chat_id=chat_id,
        expected_bot_fingerprint=fingerprint, source_email_id=source_email_id,
    )


def recover_expired_advisories(conn: sqlite3.Connection) -> int:
    """A timed-out send may have reached Telegram; no automatic replay."""
    now = _now().isoformat()
    return conn.execute("""UPDATE project_advisory_outbox
        SET state='uncertain',last_error='delivery_lease_expired',
            lease_token=NULL,lease_until=NULL,updated_at=?
        WHERE state='sending' AND lease_until<=?""", (now, now)).rowcount


def _record_analysis_failure(conn: sqlite3.Connection | None, email_id: str, error: str) -> str:
    now = _now()
    with _write_scope(conn) as db:
        row = db.execute(
            "SELECT attempts,state FROM project_advisory_outbox WHERE email_id=?", (email_id,),
        ).fetchone()
        if row is None or row["state"] != "pending":
            return "claimed_elsewhere"
        attempts = row["attempts"] + 1
        state = "blocked" if attempts >= 3 else "pending"
        db.execute("""UPDATE project_advisory_outbox
            SET state=?,attempts=?,available_at=?,last_error=?,updated_at=?
            WHERE email_id=? AND state='pending'""", (
                state, attempts, (now + timedelta(minutes=5)).isoformat(),
                error, now.isoformat(), email_id,
            ))
        return state


def _imap_route_released(conn: sqlite3.Connection, email_id: str) -> bool:
    row = conn.execute(
        "SELECT source_type,route_state FROM inbound_email_work WHERE email_id=?",
        (email_id,),
    ).fetchone()
    return row is None or row[0] != "imap" or row[1] == "completed"


def prepare_project_advisory(
    email_id: str, *, conn: sqlite3.Connection | None = None,
    analyzer: Callable[[str, str, str], Advisory] | None = None,
) -> PreparedProjectAdvisory | str:
    """Read a committed snapshot and compute without holding the writer lease."""
    with _write_scope(conn) as db:
        recover_expired_advisories(db)
        row = db.execute(
            "SELECT * FROM project_advisory_outbox WHERE email_id=?", (email_id,),
        ).fetchone()
        if row is None:
            return "absent"
        if row["state"] != "pending" or row["available_at"] > _now().isoformat():
            return row["state"]
        if not _imap_route_released(db, email_id):
            return "pending"
        chat_id, fingerprint = _destination()
        if (chat_id, fingerprint) != (row["chat_id"], row["bot_fingerprint"]):
            db.execute("""UPDATE project_advisory_outbox
                SET state='blocked',last_error='recipient_changed',updated_at=?
                WHERE email_id=? AND state='pending'""", (_now().isoformat(), email_id))
            return "blocked"
        project_id, subject, body = row["project_id"], row["subject"], row["event_summary"]
        context = read_advisory_context(db, project_id)
        if context.pending_review_ids:
            now = _now()
            db.execute("""UPDATE project_advisory_outbox SET last_error='awaiting_review',
                available_at=?,updated_at=? WHERE email_id=? AND state='pending'""",
                ((now + timedelta(minutes=1)).isoformat(), now.isoformat(), email_id))
            return "pending"
    try:
        advisory = (analyzer or analyze_project_advisory)(project_id, subject, body)
    except Exception:
        logger.exception("Project advisory analysis failed for %s", email_id)
        return _record_analysis_failure(conn, email_id, "analysis_failed")
    if not isinstance(advisory, Advisory):
        return _record_analysis_failure(conn, email_id, "invalid_advisory")
    if advisory.confidence < 0.7 or not advisory.next_steps:
        with _write_scope(conn) as db:
            if read_advisory_context(db, project_id).snapshot_json != context.snapshot_json:
                return "pending"
            changed = db.execute("""UPDATE project_advisory_outbox SET state='skipped',
                advisory_json=?,updated_at=? WHERE email_id=? AND state='pending'""",
                (advisory.model_dump_json(), _now().isoformat(), email_id)).rowcount
        return "skipped" if changed else "claimed_elsewhere"
    return PreparedProjectAdvisory(
        email_id=email_id, project_id=project_id, subject=subject, chat_id=chat_id,
        bot_fingerprint=fingerprint, advisory=advisory, context=context,
        semantic_key=advisory_semantic_key(advisory, context),
    )


def deliver_project_advisory(
    email_id: str, *, conn: sqlite3.Connection | None = None,
    analyzer: Callable[[str, str, str], Advisory] | None = None,
    sender: Callable[[Advisory, str, str, int, str], dict[str, Any]] | None = None,
) -> str:
    """Reserve semantics atomically; never replay uncertain or stale advice."""
    if sender is None and analyzer is None:
        from config.settings import CONFIG
        if CONFIG.telegram.combine_email_advisory:
            with _write_scope(conn) as db:
                presentation = db.execute("""SELECT state FROM inbound_presentation_outbox
                    WHERE email_id=? AND kind='info_card'""", (email_id,)).fetchone()
            if presentation is not None and presentation[0] in {"pending", "sending"}:
                from src.email.inbound_presentation_outbox import deliver_presentation
                deliver_presentation(email_id, conn=conn)
                with _write_scope(conn) as db:
                    row = db.execute("SELECT state FROM project_advisory_outbox WHERE email_id=?",
                                     (email_id,)).fetchone()
                return row[0] if row else "absent"
    prepared = prepare_project_advisory(email_id, conn=conn, analyzer=analyzer)
    if isinstance(prepared, str):
        return prepared
    advisory = prepared.advisory
    project_id, subject = prepared.project_id, prepared.subject
    chat_id, fingerprint = prepared.chat_id, prepared.bot_fingerprint
    token = uuid4().hex
    now = _now()
    with _write_scope(conn) as db:
        if not _imap_route_released(db, email_id):
            return "pending"
        if _destination() != (chat_id, fingerprint):
            db.execute("""UPDATE project_advisory_outbox SET state='blocked',
                last_error='recipient_changed',updated_at=? WHERE email_id=? AND state='pending'""",
                (now.isoformat(), email_id))
            return "blocked"
        state = claim_prepared_advisory(db, prepared, token=token, now=now.isoformat(),
                                       lease_until=(now + timedelta(minutes=5)).isoformat())
        if state != "sending":
            return state
    try:
        result = (sender(advisory, project_id, subject, chat_id, fingerprint)
                  if sender is not None else _send(
                      advisory, project_id, subject, chat_id, fingerprint,
                      source_email_id=email_id,
                  ))
        if not isinstance(result, dict):
            result = {}
    except Exception:
        logger.exception("Project advisory delivery outcome unknown for %s", email_id)
        result = {}
    message_id = result.get("telegram_message_id")
    if result.get("sent") and type(message_id) is int and message_id > 0:
        state, error = "sent", None
    elif result.get("error") in {"budget_exhausted", "telegram_not_configured", "project_state_changed"}:
        state, error = "pending", str(result["error"])
    elif result.get("error") in {"bot_identity_changed", "recipient_changed"}:
        state, error = "blocked", str(result["error"])
    else:
        state, error = "uncertain", str(result.get("error") or "delivery_unconfirmed")
    try:
        with _write_scope(conn) as db:
            changed = db.execute("""UPDATE project_advisory_outbox
                SET state=?,available_at=?,telegram_message_id=?,last_error=?,
                    lease_token=NULL,lease_until=NULL,updated_at=?
                WHERE email_id=? AND state='sending' AND lease_token=?""", (
                    state, (now + timedelta(minutes=5)).isoformat(),
                    message_id if state == "sent" else None, error,
                    _now().isoformat(), email_id, token,
                )).rowcount
            if changed != 1:
                return "uncertain"
    except Exception:
        logger.exception("Project advisory receipt failed for %s", email_id)
        return "uncertain"
    return state


def process_pending_project_advisories(*, limit: int = 10) -> list[str]:
    from src.db.connection import scoped_write

    with scoped_write() as conn:
        recover_expired_advisories(conn)
        rows = conn.execute("""SELECT email_id FROM project_advisory_outbox
            WHERE state='pending' AND available_at<=?
              AND NOT EXISTS (SELECT 1 FROM inbound_email_work AS route
                  WHERE route.email_id=project_advisory_outbox.email_id
                    AND route.source_type='imap' AND route.route_state!='completed')
            ORDER BY available_at,email_id LIMIT ?""", (
                _now().isoformat(), max(0, min(limit, 50)),
            )).fetchall()
    return [deliver_project_advisory(row[0]) for row in rows]
