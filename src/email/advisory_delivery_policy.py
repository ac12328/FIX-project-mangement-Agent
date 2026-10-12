"""Semantic advisory delivery policy; never authorizes project mutations or I/O."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from src.models.advisory import Advisory


class ProjectAdvisoryContext(BaseModel):
    snapshot_json: str
    state: dict = Field(default_factory=dict)
    followups: list[dict] = Field(default_factory=list)
    pending_review_ids: list[str] = Field(default_factory=list)


class PreparedProjectAdvisory(BaseModel):
    email_id: str
    project_id: str
    subject: str
    chat_id: int
    bot_fingerprint: str
    advisory: Advisory
    context: ProjectAdvisoryContext
    semantic_key: str


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def read_advisory_context(conn: sqlite3.Connection, project_id: str) -> ProjectAdvisoryContext:
    """Capture committed dependencies before analysis and recheck before claiming I/O."""
    row = conn.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
    if row is None:
        raise ValueError("advisory project no longer exists")
    project = dict(row)
    reviews = [dict(r) for r in conn.execute("""SELECT update_id,decision_outcome,proposed_changes
        FROM proposed_updates WHERE project_id=? ORDER BY update_id""", (project_id,))]
    followups = [dict(r) for r in conn.execute("""SELECT followup_id,description,due_date,
        owner_side,status,escalation_level,action_revision FROM internal_followups
        WHERE project_id=? ORDER BY followup_id""", (project_id,))]
    evidence = tuple(conn.execute("""SELECT COUNT(*),MAX(rowid) FROM evidence_store
        WHERE project_id=?""", (project_id,)).fetchone())
    memory = [dict(r) for r in conn.execute("""SELECT memory_key,memory_value,updated_at
        FROM working_memory WHERE project_id=? ORDER BY memory_key""", (project_id,))]
    state = {key: project.get(key) for key in (
        "current_milestone", "project_status", "ball_with", "next_action", "target_go_live",
    )}
    return ProjectAdvisoryContext(
        snapshot_json=_json({"project": project, "reviews": reviews, "followups": followups,
                             "evidence": evidence, "memory": memory}),
        state=state, followups=followups,
        pending_review_ids=[r["update_id"] for r in reviews if r["decision_outcome"] == "needs_review"],
    )


def _normalized(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def advisory_semantic_key(advisory: Advisory, context: ProjectAdvisoryContext) -> str:
    """Ignore email IDs, citations, confidence and prose; retain actionable differences."""
    payload = {
        "purpose": "project_advisory", "state": context.state,
        "followups": context.followups,
        "steps": [{"action": _normalized(s.action), "priority": s.priority,
                   "can_auto_draft": s.can_auto_draft} for s in advisory.next_steps],
        "escalation": advisory.escalation.model_dump() if advisory.escalation else None,
        "draft": advisory.draft_suggestion.model_dump() if advisory.draft_suggestion else None,
        "questions": [_normalized(q) for q in advisory.questions_for_operator],
        "conflicts": advisory.db_conflicts,
    }
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()


def claim_prepared_advisory(conn: sqlite3.Connection, prepared: PreparedProjectAdvisory,
                            *, token: str, now: str, lease_until: str,
                            delivery_mode: str = "standalone") -> str:
    """The dedup read and send reservation must share the serialized write transaction."""
    row = conn.execute("SELECT state,project_id,chat_id,bot_fingerprint FROM project_advisory_outbox WHERE email_id=?",
                       (prepared.email_id,)).fetchone()
    if row is None or row[0] != "pending":
        return "claimed_elsewhere"
    if (row["project_id"], row["chat_id"], row["bot_fingerprint"]) != (
            prepared.project_id, prepared.chat_id, prepared.bot_fingerprint):
        return "claimed_elsewhere"
    current = read_advisory_context(conn, prepared.project_id)
    if current.snapshot_json != prepared.context.snapshot_json or current.pending_review_ids:
        conn.execute("""UPDATE project_advisory_outbox SET last_error='project_state_changed',
            updated_at=? WHERE email_id=? AND state='pending'""", (now, prepared.email_id))
        return "pending"
    scope = (prepared.project_id, prepared.chat_id, prepared.bot_fingerprint, prepared.email_id)
    # Do not reorder concurrent sends or reproduce a matching quarantined outcome.
    inflight = conn.execute("""SELECT state,semantic_key FROM project_advisory_outbox
        WHERE project_id=? AND chat_id=? AND bot_fingerprint=? AND email_id!=?
          AND state IN ('sending','uncertain') ORDER BY evaluated_at DESC""", scope).fetchall()
    if any(r[0] == "sending" for r in inflight):
        return "pending"
    if any(r[1] == prepared.semantic_key for r in inflight):
        conn.execute("""UPDATE project_advisory_outbox SET state='blocked',
            last_error='matching_delivery_uncertain',advisory_json=?,semantic_key=?,
            snapshot_json=?,updated_at=? WHERE email_id=? AND state='pending'""", (
                prepared.advisory.model_dump_json(), prepared.semantic_key,
                prepared.context.snapshot_json, now, prepared.email_id,
            ))
        return "blocked"
    latest = conn.execute("""SELECT semantic_key FROM project_advisory_outbox
        WHERE project_id=? AND chat_id=? AND bot_fingerprint=? AND email_id!=?
          AND state='sent' AND superseded_at IS NULL AND semantic_key IS NOT NULL
        ORDER BY evaluated_at DESC,email_id DESC LIMIT 1""", scope).fetchone()
    if latest and latest[0] == prepared.semantic_key:
        conn.execute("""UPDATE project_advisory_outbox SET state='skipped',
            last_error='unchanged_advisory',advisory_json=?,semantic_key=?,snapshot_json=?,
            evaluated_at=?,updated_at=? WHERE email_id=? AND state='pending'""", (
                prepared.advisory.model_dump_json(), prepared.semantic_key,
                prepared.context.snapshot_json, now, now, prepared.email_id,
            ))
        from src.repositories.activity_log_repo import insert_activity
        from src.repositories.digest_queue_repo import enqueue_digest_item
        insert_activity(conn, actor="system", surface="email", project_id=prepared.project_id,
            action_type="project_advisory_suppressed", object_ref=prepared.email_id,
            summary="Unchanged recommendation retained in audit; no additional advisory card.", touch_project=False)
        enqueue_digest_item(conn, category="email_information", title=prepared.subject,
            body="Project recommendation unchanged", project_id=prepared.project_id,
            metadata={"email_id": prepared.email_id, "reason": "unchanged_advisory"}, commit=False)
        return "skipped"
    changed = conn.execute("""UPDATE project_advisory_outbox SET state='sending',advisory_json=?,
        semantic_key=?,snapshot_json=?,evaluated_at=?,delivery_mode=?,delivery_group=?,lease_token=?,lease_until=?,
        attempts=attempts+1,updated_at=? WHERE email_id=? AND state='pending'""", (
            prepared.advisory.model_dump_json(), prepared.semantic_key, prepared.context.snapshot_json,
            now, delivery_mode, token if delivery_mode == "presentation" else None, token, lease_until, now, prepared.email_id,
        )).rowcount
    return "sending" if changed == 1 else "claimed_elsewhere"


def refresh_advisories_after_review(conn: sqlite3.Connection, *, project_id: str,
                                    update_id: str, applied_fields: dict) -> None:
    """Invalidate sent advice and durably schedule a fresh evaluation with the review commit."""
    now = datetime.now(timezone.utc).isoformat()
    has_advice = conn.execute("""SELECT 1 FROM project_advisory_outbox
        WHERE project_id=? AND state IN ('sent','sending','uncertain') LIMIT 1""", (project_id,)).fetchone()
    conn.execute("""UPDATE project_advisory_outbox SET superseded_at=?
        WHERE project_id=? AND state='sent' AND superseded_at IS NULL""", (now, project_id))
    pending = conn.execute("""UPDATE project_advisory_outbox SET available_at=?,
        last_error=NULL,updated_at=? WHERE project_id=? AND state='pending'""", (now, now, project_id)).rowcount
    if has_advice and not pending:
        from src.email.project_advisory_outbox import enqueue_project_advisory
        enqueue_project_advisory(conn, email_id=f"review:{update_id}", project_id=project_id,
            subject="Approved project update", body="Committed review outcome: " + _json(applied_fields))


def advisory_card_is_current(conn: sqlite3.Connection, *, email_id: str, project_id: str,
                             chat_id: int | None = None, bot_fingerprint: str | None = None) -> bool:
    """Old message buttons are not authority for changed project recommendations."""
    row = conn.execute("SELECT * FROM project_advisory_outbox WHERE email_id=?", (email_id,)).fetchone()
    if row is None or row["project_id"] != project_id or row["state"] != "sent":
        return False
    if row["superseded_at"] or not row["snapshot_json"]:
        return False
    if chat_id is not None and row["chat_id"] != chat_id:
        return False
    if bot_fingerprint is not None and row["bot_fingerprint"] != bot_fingerprint:
        return False
    current = read_advisory_context(conn, project_id)
    if current.pending_review_ids:
        return False
    # Fresh evidence can leave the same recommendation unchanged. Its latest
    # confirmed evaluation renews the old card without sending a duplicate.
    latest = conn.execute("""SELECT semantic_key,snapshot_json FROM project_advisory_outbox
        WHERE project_id=? AND chat_id=? AND bot_fingerprint=?
          AND state IN ('sent','skipped') AND semantic_key IS NOT NULL
          AND superseded_at IS NULL ORDER BY evaluated_at DESC,email_id DESC LIMIT 1""",
        (project_id, row["chat_id"], row["bot_fingerprint"])).fetchone()
    return bool(latest and latest[0] == row["semantic_key"]
                and latest[1] == current.snapshot_json)

