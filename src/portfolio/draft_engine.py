"""
draft_engine.py — Central proactive draft orchestration.

Entry point for all signal-triggered draft generation:
  inbound_reply    — auto, called inline from _job_email_poll
  stale_external   — auto, called from _job_proactive_drafts
  overdue_followup — tap-to-draft, called from handle_followup callback
  sla_deadline     — tap-to-draft, called from handle_sla callback

Returns draft_id on success, None if skipped (dedup / cooldown / LLM error).
"""
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

from config.settings import CONFIG
from src.email.draft_generator import generate_email_draft
from src.email.draft_session import DraftGenerationLink, create_draft
from src.email.envelope import resolve_draft_envelope
from src.events.bus import Event, event_bus
from src.llm.client import LLMParseFailure
from src.repositories.working_memory_repo import get_memory, upsert_memory
from src.telegram.draft_card_outbox import (
    complete_draft_card_attachments, deliver_draft_card, enqueue_draft_card,
)

logger = logging.getLogger(__name__)

# task_type written to drafts table per signal
_TASK_TYPE: dict[str, str] = {
    "inbound_reply": "reply_to_client",
    "stale_external": "draft_follow_up",
    "overdue_followup": "draft_follow_up",
    "sla_deadline": "sla_status_update",
}

_COOLDOWN_MEMORY_KEY = "last_proactive_draft_at"


def _attach_requested_docs(
    data_dir: str, draft_id: str, doc_keys: list[str],
    conn: sqlite3.Connection | None = None,
) -> list[str]:
    """Journal reference docs after the draft command has committed."""
    from src.db.connection import scoped_write
    from src.email.attachments import AttachmentError
    from src.email.draft_workspace import WorkspaceLockedError
    from src.email.reference_library import get_reference_doc, resolve_doc_path
    from src.services.committed_attachment_service import (
        add_committed_draft_attachment, explicit_attachment_write_scope,
    )

    write_scope = (lambda: explicit_attachment_write_scope(conn)) if conn is not None else scoped_write
    invalid: list[str] = []
    for key in doc_keys:
        doc = get_reference_doc(key)
        if doc is None:
            logger.warning("draft_engine: unknown doc_key '%s' for draft %s", key, draft_id)
            invalid.append(key)
            continue
        path = resolve_doc_path(key)
        if path is None or not path.exists():
            logger.warning("draft_engine: file missing for doc_key '%s' draft %s", key, draft_id)
            invalid.append(key)
            continue
        try:
            add_committed_draft_attachment(
                write_scope, data_dir=data_dir, draft_id=draft_id,
                filename=doc["filename"], data=path.read_bytes(),
                actor_user_id="system:proactive-draft",
            )
        except (AttachmentError, OSError, ValueError, WorkspaceLockedError):
            logger.warning(
                "draft_engine: attachment failed for '%s' on draft %s", key, draft_id
            )
            invalid.append(key)
    return invalid


def generate_draft_for_signal(
    conn: sqlite3.Connection | None,
    signal_type: str,
    project_id: str,
    context_hints: dict,
    chat_id: int,
) -> str | None:
    """
    Generate a proactive draft for a signal. Returns draft_id or None if skipped.

    ``conn=None`` owns a short pooled write scope. An explicit idle connection
    owns its own draft/cooldown transaction; an active caller transaction is
    rejected rather than committed implicitly. Its committed card intent is
    delivered later by the outbox worker, not via the process-global pool.

    Deduplication: skips if a pending draft of the same task_type already exists.
    Cooldown: stale_external only — skips if a draft was generated within 7 days.
    """
    from src.db.connection import scoped_read, scoped_write

    if conn is not None and conn.in_transaction:
        raise ValueError("Explicit draft connection has an active transaction")

    task_type = _TASK_TYPE.get(signal_type, "draft_follow_up")

    # Phase 1: Read checks (dedup + cooldown) — read access only
    if conn is not None:
        _rconn = conn
    else:
        _rconn = None

    def _check_skip(rconn):
        # 1. Deduplication — one pending draft per project+task_type
        existing = rconn.execute(
            "SELECT draft_id FROM drafts WHERE project_id = ? AND task_type = ? "
            "AND status IN ('created', 'in_review', 'approved') LIMIT 1",
            (project_id, task_type),
        ).fetchone()
        if existing:
            logger.debug(
                "draft_engine: skip %s for %s — pending draft %s exists",
                signal_type, project_id, existing["draft_id"],
            )
            return True
        # 2. Cooldown (stale_external only)
        if signal_type == "stale_external":
            mem = get_memory(rconn, project_id, _COOLDOWN_MEMORY_KEY)
            if mem and mem.get("memory_value"):
                try:
                    last_at = datetime.fromisoformat(mem["memory_value"])
                    if last_at.tzinfo is None:
                        last_at = last_at.replace(tzinfo=timezone.utc)
                    cooldown_days = CONFIG.thresholds.proactive_cooldown_days
                    if datetime.now(timezone.utc) - last_at < timedelta(days=cooldown_days):
                        logger.debug(
                            "draft_engine: cooldown active for %s — last draft %s",
                            project_id, mem["memory_value"],
                        )
                        return True
                except ValueError:
                    pass
        return False

    if _rconn is not None:
        if _check_skip(_rconn):
            return None
    else:
        with scoped_read() as rconn:
            if _check_skip(rconn):
                return None

    # Phase 2: LLM draft generation — uses read access internally
    reply_to_email_id: str | None = None
    try:
        if conn is not None:
            generated = generate_email_draft(
                conn, project_id, signal_type, context_hints
            )
            subject, body, to_email = generated
            try:
                envelope = resolve_draft_envelope(conn, project_id, for_reply=True)
                reply_to_email_id = envelope.reply_to_email_id
            except Exception:
                pass
        else:
            with scoped_read() as rconn:
                generated = generate_email_draft(
                    rconn, project_id, signal_type, context_hints
                )
                subject, body, to_email = generated
                try:
                    envelope = resolve_draft_envelope(rconn, project_id, for_reply=True)
                    reply_to_email_id = envelope.reply_to_email_id
                except Exception:
                    pass
    except LLMParseFailure as exc:
        logger.warning("draft_engine: LLM failed for %s/%s: %s", signal_type, project_id, exc)
        return None
    except Exception:
        logger.exception("draft_engine: unexpected error generating draft for %s", project_id)
        return None

    requested_docs = context_hints.get("requested_docs") or []

    # Phase 3: Persist draft — needs write access (short-lived lock)
    def _create(wconn, *, commit=True, post_commit=None):
        return create_draft(
            wconn,
            project_id,
            task_type,
            subject,
            body,
            to_email=to_email,
            context_packet_ref=reply_to_email_id,
            generation_link=(
                DraftGenerationLink(generated.trace_id, generated.output_revision_id)
                if getattr(generated, "trace_id", None)
                and getattr(generated, "output_revision_id", None) else None
            ),
            commit=commit,
            post_commit=post_commit,
        )

    def _stage(wconn, effects):
        # Recheck while holding this command's write lease. A competing draft
        # or completed project can invalidate the earlier model input.
        if _check_skip(wconn):
            return None
        if signal_type == "stale_external":
            current = wconn.execute(
                "SELECT project_status, ball_with FROM projects WHERE project_id=?",
                (project_id,),
            ).fetchone()
            if not current or current["project_status"] != "Active" or current["ball_with"] != "External":
                logger.info("draft_engine: skip stale draft after project state changed for %s", project_id)
                return None
        draft_id = _create(wconn, commit=False, post_commit=effects)
        enqueue_draft_card(
            wconn, draft_id=draft_id, to_email=to_email or "",
            subject=subject, chat_id=chat_id,
            attachments_pending=bool(requested_docs),
        )
        if signal_type == "stale_external":
            upsert_memory(
                wconn, project_id, _COOLDOWN_MEMORY_KEY,
                datetime.now(timezone.utc).isoformat(),
            )
        return draft_id

    # An explicit idle connection is a self-owned command, not a nested caller
    # transaction. Refuse a pre-existing transaction rather than committing
    # unrelated caller writes or sending a card before their commit.
    effects = []
    try:
        if conn is not None:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                draft_id = _stage(conn, effects)
        else:
            with scoped_write() as wconn:
                draft_id = _stage(wconn, effects)
    except Exception:
        logger.exception("draft_engine: draft/cooldown transaction failed for %s", project_id)
        return None
    if not draft_id:
        return None

    for effect in effects:
        try:
            effect()
        except Exception:
            logger.exception("draft_engine: post-commit effect failed for draft %s", draft_id)

    invalid_keys = []
    if requested_docs:
        try:
            invalid_keys = _attach_requested_docs(CONFIG.data_dir, draft_id, requested_docs, conn)
        except Exception:
            logger.exception("draft_engine: attachment attempt failed for committed draft %s", draft_id)
            invalid_keys = requested_docs
    card_subject = subject
    if invalid_keys:
        card_subject = f"{subject}\n\u26a0\ufe0f Requested docs not attached or unverified: {', '.join(invalid_keys)}"
    try:
        if requested_docs:
            if conn is None:
                with scoped_write() as wconn:
                    complete_draft_card_attachments(wconn, draft_id, card_subject)
            else:
                # Stay on the supplied database, which may be isolated from
                # the process-global pool. The worker cannot claim preparation.
                with conn:
                    complete_draft_card_attachments(conn, draft_id, card_subject)
        if conn is None:
            # A committed, recipient-bound, attachment-ready intent is claimed
            # before network I/O. Failed preparation remains non-deliverable.
            deliver_draft_card(draft_id)
        # Explicit connections leave delivery to the scheduled outbox worker.
    except Exception:
        logger.exception("draft_engine: card delivery deferred for committed draft %s", draft_id)
    try:
        event_bus.publish(Event(
            event_type="draft_created",
            project_id=project_id,
            detail={"draft_id": draft_id, "task_type": task_type, "signal_type": signal_type},
            surface="system",
        ))
    except Exception:
        logger.exception("draft_engine: SSE publication failed for committed draft %s", draft_id)
    logger.info("draft_engine: created %s for %s (signal=%s)", draft_id, project_id, signal_type)
    return draft_id
