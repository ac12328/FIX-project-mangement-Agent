"""Atomic, source-preserving delivery of one email card and its project advice."""
from __future__ import annotations

import json
import hashlib
import logging
import sqlite3
from datetime import timedelta
from typing import Callable
from uuid import uuid4

from src.email.advisory_delivery_policy import PreparedProjectAdvisory, claim_prepared_advisory
from src.email.analysis_notice import EmailAnalysisNotice

logger = logging.getLogger(__name__)


def merge_advisory_notice(payload: dict, prepared: PreparedProjectAdvisory) -> dict:
    """Keep the email-specific next step and all attachment coverage disclosures."""
    notice = EmailAnalysisNotice.model_validate(payload["notice"])
    notice.current_state = " / ".join(str(prepared.context.state.get(k) or "") for k in (
        "current_milestone", "project_status", "ball_with"))
    steps = [step.action for step in prepared.advisory.next_steps]
    if not notice.next_step and steps:
        notice.next_step = steps.pop(0)
    notice.advisory_steps = [s for s in steps if s.strip().casefold() != notice.next_step.strip().casefold()]
    if prepared.advisory.escalation:
        escalation = prepared.advisory.escalation
        notice.advisory_escalation = f"{escalation.team}: {escalation.reason}"
    if prepared.advisory.draft_suggestion:
        notice.advisory_draft = prepared.advisory.draft_suggestion.purpose
    notice.advisory_questions = list(prepared.advisory.questions_for_operator)
    notice.advisory_conflicts = list(prepared.advisory.db_conflicts)
    return {**payload, "notice": notice.model_dump(), "combined_advisory": True,
            "advisory_snapshot": prepared.context.snapshot_json,
            "advisory_semantic_key": prepared.semantic_key,
            "advisory_fingerprint": hashlib.sha256(prepared.advisory.model_dump_json().encode()).hexdigest(),
            "advisory_urgent": bool(prepared.advisory.escalation)}


def try_combined_presentation(
    email_id: str, *, conn: sqlite3.Connection | None = None,
    sender: Callable[[str, dict, int, str], dict] | None = None,
) -> str | None:
    """None falls back to the ordinary source card; never drops a new email."""
    from config.settings import CONFIG
    from src.email.project_advisory_outbox import (
        _destination, _imap_route_released, _now, _write_scope, prepare_project_advisory,
        recover_expired_advisories,
    )
    from src.email.inbound_presentation_outbox import _send, recover_expired_presentations

    if not CONFIG.telegram.combine_email_advisory:
        return None
    with _write_scope(conn) as db:
        recover_expired_advisories(db)
        recover_expired_presentations(db)
        presentation = db.execute("""SELECT * FROM inbound_presentation_outbox
            WHERE email_id=? AND kind='info_card'""", (email_id,)).fetchone()
        advisory = db.execute("SELECT state,project_id FROM project_advisory_outbox WHERE email_id=?",
                              (email_id,)).fetchone()
        if (presentation is None or presentation["state"] != "pending"
                or advisory is None or advisory[0] != "pending"
                or advisory["project_id"] != presentation["project_id"]):
            return None
        if presentation["available_at"] > _now().isoformat() or not _imap_route_released(db, email_id):
            return "pending"
    prepared = prepare_project_advisory(email_id, conn=conn)
    if not isinstance(prepared, PreparedProjectAdvisory):
        return None
    now = _now()
    token = uuid4().hex
    with _write_scope(conn) as db:
        row = db.execute("SELECT * FROM inbound_presentation_outbox WHERE email_id=?",
                         (email_id,)).fetchone()
        if row["state"] != "pending":
            return row["state"]
        if row["available_at"] > now.isoformat() or not _imap_route_released(db, email_id):
            return "pending"
        if row["project_id"] != prepared.project_id:
            return None
        source_payload = json.loads(row["payload_json"])
        if source_payload.get("project_id") != prepared.project_id:
            return None
        destination = (prepared.chat_id, prepared.bot_fingerprint)
        if _destination() != destination or (row["chat_id"], row["bot_fingerprint"]) != destination:
            return None  # Ordinary dispatch records its own recipient-change block.
        state = claim_prepared_advisory(db, prepared, token=token, now=now.isoformat(),
            lease_until=(now + timedelta(minutes=5)).isoformat(), delivery_mode="presentation")
        if state != "sending":
            return None
        payload = {**merge_advisory_notice(source_payload, prepared), "combined_delivery_group": token}
        changed = db.execute("""UPDATE inbound_presentation_outbox SET state='sending',
            lease_token=?,lease_until=?,attempts=attempts+1,delivery_payload_json=?,updated_at=?
            WHERE email_id=? AND state='pending'""", (
                token, (now + timedelta(minutes=5)).isoformat(), json.dumps(payload, ensure_ascii=False), now.isoformat(), email_id,
            )).rowcount
        if changed != 1:
            raise RuntimeError("combined presentation claim lost")
    try:
        result = (sender or _send)("info_card", payload, prepared.chat_id, prepared.bot_fingerprint)
        if not isinstance(result, dict):
            result = {}
    except Exception:
        logger.exception("Combined email delivery outcome unknown for %s", email_id)
        result = {}
    message_id = result.get("telegram_message_id")
    if result.get("sent") and type(message_id) is int and message_id > 0:
        state, error = "sent", None
    elif result.get("error") in {"budget_exhausted", "telegram_not_configured", "project_state_changed"}:
        state, error = "pending", result["error"]
    elif result.get("error") in {"bot_identity_changed", "recipient_changed"}:
        state, error = "blocked", result["error"]
    else:
        state, error = "uncertain", str(result.get("error") or "delivery_unconfirmed")
    try:
        with _write_scope(conn) as db:
            for table in ("project_advisory_outbox", "inbound_presentation_outbox"):
                changed = db.execute(f"""UPDATE {table} SET state=?,available_at=?,
                    telegram_message_id=?,last_error=?,lease_token=NULL,lease_until=NULL,updated_at=?
                    WHERE email_id=? AND state='sending' AND lease_token=?""", (
                        state, (now + timedelta(minutes=5)).isoformat(), message_id if state == "sent" else None,
                        error, _now().isoformat(), email_id, token,
                    )).rowcount
                if changed != 1:
                    raise RuntimeError("combined receipt claim lost")
    except Exception:
        logger.exception("Combined email receipt failed for %s", email_id)
        return "uncertain"
    return state
