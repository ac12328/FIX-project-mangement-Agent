"""
Lead Agent analysis for inbound client replies (Blueprint Section 27.3).

Generates a concise summary and suggested_action for a detected client reply,
then stores an InboundReply record.
"""

import logging
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections.abc import Callable
from uuid import uuid4

from config.defaults import EMAIL_BODY_SNIPPET_CHARS, EMAIL_BODY_STORE_CHARS
from src.email.analysis_guards import (
    adjust_email_analysis_result,
    wiki_queries_for_email_body,
)
from config.settings import CONFIG
from src.email.inbox_fetcher import FetchedEmail
from src.email.inbound_work import ProjectFieldReviewCommandReceipt
from src.llm.client import llm_call_structured
from src.models.inbound_reply import InboundReply
from src.repositories.activity_log_repo import insert_activity
from src.repositories.inbound_reply_repo import insert_inbound_reply, make_reply_id
from src.repositories.resolution_pattern_repo import search_patterns
from src.utils.prompt_loader import load_prompt

logger = logging.getLogger(__name__)


def analyse_and_store_reply(
    conn: sqlite3.Connection | None,
    fetched: FetchedEmail,
    project_id: str,
    *,
    from_contact_id: str | None = None,
    original_draft_id: str | None = None,
    email_type: str = "project_update",
    skip_auto_write: bool = False,
    post_commit: list[Callable[[], None]] | None = None,
) -> tuple[InboundReply, dict]:
    """
    Run Lead Agent analysis on an inbound reply and persist it.

    When ``conn`` is None the function manages its own connections so that
    no write lock is held during LLM calls (which run in a thread pool).

    Returns (InboundReply, proposed_fields) where proposed_fields is a dict
    of project field changes the LLM suggests (may be empty).
    """
    # Run analysis and commitment extraction in parallel (B2 optimisation)
    from src.email.commitment_extractor import extract_commitment, create_followup_from_commitment

    body_snippet = (fetched.body or "")[:EMAIL_BODY_SNIPPET_CHARS]
    analysis_result = None
    commitment = None

    # Phase 1: LLM analysis — needs read access only.
    # Use provided conn for reads, or open scoped_read when conn is None.
    _read_conn = conn
    if _read_conn is None:
        from src.db.connection import scoped_read
        _read_conn_cm = scoped_read()
    else:
        from contextlib import nullcontext
        _read_conn_cm = nullcontext(_read_conn)

    with _read_conn_cm as rconn:
        # sqlite3 connections supplied by tests (and scoped connections) are
        # thread-affine. Gather DB-backed context before handing LLM work to a
        # worker, so the worker never substitutes an unrelated default DB.
        analysis_context = _prepare_analysis_context(
            rconn, fetched, project_id, email_type=email_type,
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            future_analysis = pool.submit(
                _run_analysis, rconn, fetched, project_id, email_type,
                analysis_context=analysis_context,
            )
            future_commitment = pool.submit(extract_commitment, project_id, body_snippet)

            for fut in as_completed([future_analysis, future_commitment]):
                if fut is future_analysis:
                    try:
                        analysis_result = fut.result()
                    except Exception:
                        logger.exception("Reply analysis failed in parallel executor")
                else:
                    try:
                        commitment = fut.result()
                    except Exception:
                        logger.exception("Commitment extraction failed in parallel executor")

    # If analysis failed entirely, use fallback
    if analysis_result is None:
        subject = fetched.subject or "(no subject)"
        analysis_result = (
            f"Reply received from {fetched.sender_email}: {subject}",
            f"Review inbound reply from {fetched.sender_email} for project {project_id}.",
            0.5,
            {},
            [],
        )

    summary, suggested_action, confidence, proposed_fields, requested_docs = analysis_result

    reply = InboundReply(
        reply_id=make_reply_id(),
        thread_id=fetched.in_reply_to,
        project_id=project_id,
        from_contact_id=from_contact_id,
        received_at=fetched.received_at,
        subject=fetched.subject,
        body_text=fetched.body[:EMAIL_BODY_STORE_CHARS] if fetched.body else None,
        original_draft_id=original_draft_id,
        summary=summary,
        suggested_action=suggested_action,
        confidence=confidence,
        requested_docs=requested_docs,
    )

    # Phase 2: Persist — needs write access.
    if conn is not None:
        _persist_reply(conn, reply, fetched, project_id, commitment, proposed_fields,
                       skip_auto_write, post_commit=post_commit)
    else:
        from src.db.connection import scoped_write
        effects: list[Callable[[], None]] = []
        with scoped_write() as wconn:
            _persist_reply(wconn, reply, fetched, project_id, commitment, proposed_fields,
                           skip_auto_write, post_commit=effects)
        _dispatch_reply_effects(effects)

    return reply, proposed_fields


def _persist_reply(
    conn: sqlite3.Connection,
    reply: InboundReply,
    fetched: FetchedEmail,
    project_id: str,
    commitment: dict | None,
    proposed_fields: dict,
    skip_auto_write: bool,
    *,
    post_commit: list[Callable[[], None]] | None = None,
) -> None:
    """Persist one reply command without committing a caller-owned transaction."""
    owns_transaction = not conn.in_transaction
    if owns_transaction:
        conn.execute("BEGIN")
    savepoint = "reply_command_" + uuid4().hex
    effects: list[Callable[[], None]] = []
    conn.execute("SAVEPOINT " + savepoint)
    savepoint_open = True
    try:
        insert_inbound_reply(conn, reply)
        insert_activity(
            conn, actor="email_poller", action_type="inbound_reply",
            project_id=project_id, surface="email",
            summary=f"Client reply from {fetched.sender_email}: {fetched.subject or '(no subject)'}",
        )
        if reply.original_draft_id:
            def _attribute() -> None:
                from src.learning.evaluation.producer_outcomes import attribute_client_reply
                attribute_client_reply(
                    conn, original_draft_id=reply.original_draft_id, reply_id=reply.reply_id,
                )
            _optional_reply_step(conn, "attribution", effects, _attribute, reply.reply_id)

        if commitment and commitment.get("has_commitment"):
            def _handle_commitment() -> None:
                from src.email.commitment_extractor import create_followup_from_commitment
                if getattr(CONFIG, "commitment_requires_confirmation", True):
                    from src.email.commitment_card_outbox import (
                        enqueue_commitment_card, deliver_commitment_card,
                    )
                    enqueue_commitment_card(
                        conn, reply_id=reply.reply_id, project_id=project_id,
                        commitment_text=commitment.get("commitment_text") or "Client commitment",
                        due_date=commitment.get("due_date") or "",
                    )
                    effects.append(lambda: deliver_commitment_card(
                        reply.reply_id, conn=conn if owns_transaction else None,
                    ))
                else:
                    fup_id = create_followup_from_commitment(
                        conn, project_id, commitment, commit=False,
                    )
                    if fup_id:
                        logger.info("Auto-created follow-up %s from commitment in %s",
                                    fup_id, reply.reply_id)

            _optional_reply_step(conn, "commitment", effects, _handle_commitment, reply.reply_id)

        threshold = CONFIG.write_layer.auto_write_confidence_threshold
        if reply.confidence >= threshold and proposed_fields and not skip_auto_write:
            def _route_auto_write() -> None:
                from src.email.auto_apply_undo import (
                    auto_apply_perfect_with_undo_card, can_auto_apply, is_perfect_confidence,
                )
                if is_perfect_confidence(reply.confidence) and can_auto_apply(proposed_fields):
                    auto_apply_perfect_with_undo_card(
                        conn, project_id=project_id, proposed_fields=proposed_fields,
                        confidence=reply.confidence, evidence_ref=reply.reply_id,
                        actor="email_pipeline", reason=f"Auto from email: {reply.suggested_action[:100]}",
                        summary=reply.summary or "", suggested_action=reply.suggested_action or "",
                        sender_email=fetched.sender_email, post_commit=effects,
                    )
                elif CONFIG.write_layer.auto_write_requires_confirmation:
                    _push_auto_write_review(
                        conn, project_id, reply, fetched, proposed_fields, post_commit=effects,
                        dispatch_conn=conn if owns_transaction else None,
                    )
                else:
                    _execute_direct_auto_write(
                        conn, project_id, reply, fetched, proposed_fields, post_commit=effects,
                    )

            _optional_reply_step(conn, "auto-write", effects, _route_auto_write, reply.reply_id)

        conn.execute("RELEASE SAVEPOINT " + savepoint)
        savepoint_open = False
        if owns_transaction:
            conn.commit()
    except Exception:
        try:
            if savepoint_open and conn.in_transaction:
                conn.execute("ROLLBACK TO SAVEPOINT " + savepoint)
                conn.execute("RELEASE SAVEPOINT " + savepoint)
        finally:
            if owns_transaction:
                conn.rollback()
        raise

    if owns_transaction:
        _dispatch_reply_effects(effects)
    elif post_commit is not None:
        post_commit.extend(effects)


def _optional_reply_step(
    conn: sqlite3.Connection, name: str, effects: list[Callable[[], None]],
    action: Callable[[], None], reply_id: str,
) -> None:
    """A failed optional step must not erase the reply or caller's prior work."""
    savepoint = "reply_" + uuid4().hex
    effect_count = len(effects)
    conn.execute("SAVEPOINT " + savepoint)
    try:
        action()
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT " + savepoint)
        del effects[effect_count:]
        logger.exception("Reply %s step failed for %s", name, reply_id)
    finally:
        conn.execute("RELEASE SAVEPOINT " + savepoint)


def _dispatch_reply_effects(effects: list[Callable[[], None]]) -> None:
    for effect in effects:
        try:
            effect()
        except Exception:
            logger.exception("Post-commit reply effect failed")


def _push_auto_write_review(
    conn: sqlite3.Connection,
    project_id: str,
    reply: InboundReply,
    fetched: FetchedEmail,
    proposed_fields: dict,
    *,
    post_commit: list[Callable[[], None]] | None = None,
    dispatch_conn: sqlite3.Connection | None = None,
) -> None:
    """Create a proposed_update and push a review card for operator confirmation."""
    from src.models.proposed_update import ProposedUpdate, ProposedChange, WriteDecision
    from src.models.enums import TaskType, RiskLevel, WriteDecisionOutcome
    from src.repositories.proposed_update_repo import store_proposed_update
    from src.repositories.project_repo import get_project_field
    from src.email.review_notification import queue_review_notification
    from src.write_layer.write_helpers import fields_unchanged

    proposed_fields = fields_unchanged(conn, project_id, proposed_fields)
    if not proposed_fields:
        logger.info("Skipped idempotent legacy email review for %s", project_id)
        return

    changes = [
        ProposedChange(
            field=field,
            old_value=(
                str(current) if (current := get_project_field(conn, project_id, field)) is not None
                else None
            ),
            new_value=str(value) if value is not None else None,
            reason=f"Detected in email from {fetched.sender_email}",
            evidence_ref=reply.reply_id,
        )
        for field, value in proposed_fields.items()
    ]

    update = ProposedUpdate(
        project_id=project_id,
        proposed_changes=changes,
        task_type=TaskType.TRUTH_WRITE_REASONING,
        confidence=reply.confidence,
        risk_level=RiskLevel.MEDIUM,
        requires_operator_review=True,
    )

    # Store with needs_review decision (operator must approve)
    decision = WriteDecision(
        update_id=update.update_id,
        project_id=project_id,
        outcome=WriteDecisionOutcome.NEEDS_REVIEW,
    )
    delivery_outcome = queue_review_notification(
        conn, project_id=project_id, update_id=update.update_id,
        store_proposal=lambda: store_proposed_update(conn, update, decision),
        payload={
            "project_id": project_id,
            "update_id": update.update_id,
            "summary": (
                f"Email auto-write: {reply.suggested_action[:100]}"
                if reply.suggested_action else "Email auto-write proposal"
            ),
            "reason": f"Email from {fetched.sender_email} (conf={reply.confidence:.2f})",
            "risk_level": "MEDIUM",
            "evidence_refs": [reply.reply_id],
            "proposed_changes": [change.model_dump() for change in changes],
        },
    )
    logger.info(
        "L2 auto-write routed to review for %s (update_id=%s, delivery=%s)",
        project_id, update.update_id, delivery_outcome,
    )
    if post_commit is not None:
        from src.email.update_notification_outbox import deliver_update_notification
        post_commit.append(lambda: deliver_update_notification(update.update_id, conn=dispatch_conn))


def _execute_direct_auto_write(
    conn: sqlite3.Connection,
    project_id: str,
    reply: InboundReply,
    fetched: FetchedEmail,
    proposed_fields: dict,
    *,
    post_commit: list[Callable[[], None]] | None = None,
) -> None:
    """Legacy direct write path, after the caller's reply persistence."""
    from src.email.direct_project_write import apply_direct_project_update

    write_result = apply_direct_project_update(
        conn, project_id=project_id, fields=proposed_fields, actor="email_pipeline",
        reason=f"Auto from email: {reply.suggested_action[:100]}",
        evidence_ref=reply.reply_id, confidence=reply.confidence, fetched=fetched,
        post_commit=post_commit,
    )
    if write_result.outcome == "needs_review":
        logger.info(
            "L2 auto-write requires review for %s: %s",
            project_id, write_result.needs_review_reason,
        )
    elif write_result.outcome != "accepted":
        logger.warning(
            "L2 auto-write rejected or unchanged for %s: %s",
            project_id, write_result.rejection_reason,
        )


def route_proposed_field_updates(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    proposed_fields: dict,
    confidence: float,
    evidence_ref: str,
    actor: str,
    reason: str,
    summary: str = "",
    suggested_action: str = "",
    sender_email: str = "",
    email_body: str | None = None,
    email_intent: str = "",
    has_attachments: bool = False,
    subject: str | None = None,
    assessment: dict | None = None,
    policy_version: str | None = None,
    source_type: str | None = None,
    source_ref: str | None = None,
    post_commit: list[Callable[[], None]] | None = None,
    raise_on_error: bool = False,
    review_commands: list[ProjectFieldReviewCommandReceipt] | None = None,
    effect_commands: list | None = None,
) -> str:
    """Apply the same auto-write gate used by inbound reply analysis.

    Returns: ``review_card``, ``review_notification_pending``,
    ``review_notification_unknown``, ``auto_applied_undo_card``, ``direct_write``,
    ``auto_applied_notification_pending``, ``auto_applied_notification_unknown``,
    ``below_review_threshold``, or ``skipped``.

    D3: ``current_milestone`` never silent-auto-applies — always review card.
    """
    if source_ref and (proposed_fields or {}).get("current_milestone") == "M8_GoLive":
        from src.email.pct_completion_routing import try_complete_pct
        pct_result = try_complete_pct(conn, project_id, source_ref, post_commit=post_commit)
        if pct_result:
            from src.email.pct_completion_routing import without_completed_pct_fields
            proposed_fields = without_completed_pct_fields(proposed_fields)
            if not proposed_fields:
                return "pct_completed" if pct_result["outcome"] == "applied" else "skipped"

    if not proposed_fields:
        return "skipped"

    confidence, proposed_fields = adjust_email_analysis_result(
        email_body,
        confidence,
        proposed_fields,
        sender_email=sender_email,
        email_intent=email_intent,
        has_attachments=has_attachments,
        assessment=assessment, project_id=project_id, source_ref=source_ref,
    )
    if not proposed_fields:
        return "skipped"

    from src.write_layer.write_helpers import fields_unchanged

    proposed_fields = fields_unchanged(conn, project_id, proposed_fields)
    if not proposed_fields:
        logger.info("Skipped idempotent email field proposal for %s", project_id)
        return "skipped"

    review_threshold = CONFIG.write_layer.confidence_auto_threshold
    auto_write_threshold = CONFIG.write_layer.auto_write_confidence_threshold
    if confidence < review_threshold:
        return "below_review_threshold"

    def _review() -> str:
        try:
            return _push_proposed_fields_review(
                conn,
                project_id=project_id,
                proposed_fields=proposed_fields,
                confidence=confidence,
                evidence_ref=evidence_ref,
                summary=summary,
                suggested_action=suggested_action,
                sender_email=sender_email,
                reason=reason,
                subject=subject,
                assessment=assessment,
                policy_version=policy_version,
                source_type=source_type,
                source_ref=source_ref,
                review_commands=review_commands,
            )
        except Exception:
            logger.exception("Review proposal persistence failed for %s", project_id)
            return "review_persistence_failed"

    # D3: milestone is high-stakes — always operator review, and newly generated
    # reviews can require a validated evidence-grounded assessment during rollout.
    if "current_milestone" in proposed_fields:
        from src.email.project_state_assessment import milestone_review_allowed

        if not milestone_review_allowed(
            proposed_fields,
            assessment,
            legacy=False,
            project_id=project_id,
            source_type=source_type,
            source_ref=source_ref,
        ):
            return "assessment_required"
        return _review()

    # Moderate-confidence truth changes are reviewable, but never auto-applied.
    if confidence < auto_write_threshold:
        return _review()

    try:
        from src.email.inbound_field_effect_identity import (
            export_field_effect_command, snapshot_field_effect_ids,
        )
        before = snapshot_field_effect_ids(conn, project_id) if effect_commands is not None else None

        def _effect(outcome):
            if effect_commands is not None and outcome in {"direct_write", "auto_applied_notification_pending"}:
                effect_commands.append(export_field_effect_command(
                    conn, before=before, project_id=project_id, source_ref=source_ref,
                    evidence_ref=evidence_ref, proposed_fields=proposed_fields, outcome=outcome,
                ))
            return outcome

        from src.email.auto_apply_undo import (
            auto_apply_perfect_with_undo_card,
            can_auto_apply,
            is_perfect_confidence,
        )
        if is_perfect_confidence(confidence) and can_auto_apply(proposed_fields):
            from src.utils.working_hours import is_within_working_hours

            if not is_within_working_hours(CONFIG):
                return _review()
            return _effect(auto_apply_perfect_with_undo_card(
                conn,
                project_id=project_id,
                proposed_fields=proposed_fields,
                confidence=confidence,
                evidence_ref=evidence_ref,
                actor=actor,
                reason=reason,
                summary=summary,
                suggested_action=suggested_action,
                sender_email=sender_email,
                post_commit=post_commit,
            ))
        if CONFIG.write_layer.auto_write_requires_confirmation:
            return _review()
        write_result = _execute_direct_proposed_write(
            conn,
            project_id=project_id,
            proposed_fields=proposed_fields,
            confidence=confidence,
            evidence_ref=evidence_ref,
            actor=actor,
            reason=reason,
            sender_email=sender_email,
            post_commit=post_commit,
        )
        return _effect("direct_write") if write_result.outcome == "accepted" else "skipped"
    except Exception:
        logger.exception(
            "Proposed field routing failed for %s (evidence=%s)",
            project_id,
            evidence_ref,
        )
        if raise_on_error:
            raise
        return "skipped"


def _push_proposed_fields_review(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    proposed_fields: dict,
    confidence: float,
    evidence_ref: str,
    summary: str,
    suggested_action: str,
    sender_email: str,
    reason: str,
    subject: str | None = None,
    assessment: dict | None = None,
    policy_version: str | None = None,
    source_type: str | None = None,
    source_ref: str | None = None,
    review_commands: list[ProjectFieldReviewCommandReceipt] | None = None,
) -> str:
    """Persist a review proposal and its delivery intent before Telegram I/O."""
    from src.models.proposed_update import ProposedUpdate, ProposedChange, WriteDecision
    from src.models.enums import TaskType, RiskLevel, WriteDecisionOutcome
    from src.repositories.project_repo import get_project, get_project_field
    from src.repositories.proposed_update_repo import store_proposed_update
    from src.email.review_notification import queue_review_notification

    changes = [
        ProposedChange(
            field=field,
            old_value=(
                str(current) if (current := get_project_field(conn, project_id, field)) is not None
                else None
            ),
            new_value=str(value) if value is not None else None,
            reason=reason or f"Detected in email from {sender_email}",
            evidence_ref=evidence_ref,
        )
        for field, value in proposed_fields.items()
    ]
    update = ProposedUpdate(
        project_id=project_id,
        proposed_changes=changes,
        task_type=TaskType.TRUTH_WRITE_REASONING,
        confidence=confidence,
        risk_level=RiskLevel.MEDIUM,
        requires_operator_review=True,
        assessment=assessment,
        policy_version=policy_version,
        source_type=source_type,
        source_ref=source_ref,
    )
    decision = WriteDecision(
        update_id=update.update_id,
        project_id=project_id,
        outcome=WriteDecisionOutcome.NEEDS_REVIEW,
    )
    client_name = None
    try:
        proj = get_project(conn, project_id)
        if proj:
            client_name = proj.client_name
    except Exception:
        pass

    delivery_outcome = queue_review_notification(
        conn, project_id=project_id, update_id=update.update_id,
        store_proposal=lambda: store_proposed_update(conn, update, decision),
        payload={
            "project_id": project_id,
            "update_id": update.update_id,
            "summary": summary or suggested_action or "Review proposed email update",
            "reason": f"From {sender_email}" if sender_email else reason,
            "risk_level": "MEDIUM",
            "evidence_refs": [evidence_ref] if evidence_ref else None,
            "proposed_changes": [change.model_dump() for change in changes],
            "sender_email": sender_email or None,
            "subject": subject,
            "client_name": client_name,
            "confidence": confidence,
            "suggested_action": suggested_action or "",
        },
    )
    if review_commands is not None:
        import hashlib

        source = conn.execute(
            "SELECT sender_email,body_snippet FROM processed_emails WHERE email_id=?",
            (source_ref,),
        ).fetchone()
        if source is None:
            raise ValueError("Claimed field review requires its persisted email source")
        source_hash = hashlib.sha256(
            ((source[0] or "") + "\0" + (source[1] or "")).encode("utf-8"),
        ).hexdigest()
        review_commands.append(ProjectFieldReviewCommandReceipt(
            project_id=project_id, source_ref=source_ref, source_hash=source_hash,
            write_action=delivery_outcome, evidence_id=evidence_ref,
            update_id=update.update_id, delivery_intent_id=update.update_id,
            proposed_fields={change.field: change.new_value for change in changes},
        ))
    try:
        logger.info(
            "Inbound proposed fields routed to review for %s (update_id=%s, delivery=%s)",
            project_id, update.update_id, delivery_outcome,
        )
    except Exception:
        # Logging after the durable commit cannot turn a successful review into
        # review_persistence_failed; callers would then delete its evidence.
        pass
    return delivery_outcome


def _execute_direct_proposed_write(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    proposed_fields: dict,
    confidence: float,
    evidence_ref: str,
    actor: str,
    reason: str,
    sender_email: str = "",
    post_commit: list[Callable[[], None]] | None = None,
) -> None:
    """Direct write path when operator confirmation is disabled."""
    from src.email.direct_project_write import apply_direct_project_update

    result = apply_direct_project_update(
        conn, project_id=project_id, fields=proposed_fields,
        actor=actor, reason=reason, evidence_ref=evidence_ref,
        confidence=confidence, post_commit=post_commit,
    )
    if result.outcome == "rejected":
        logger.warning(
            "Direct proposed write rejected for %s: %s",
            project_id, result.rejection_reason,
        )
    return result


def _run_analysis(
    conn: sqlite3.Connection,
    fetched: FetchedEmail,
    project_id: str,
    email_type: str,
    *,
    analysis_context: dict | None = None,
) -> tuple[str, str, float, dict, list]:
    """Return (summary, suggested_action, confidence, proposed_fields, requested_docs)."""
    try:
        body_snippet = (fetched.body or "")[:EMAIL_BODY_SNIPPET_CHARS]
        context = analysis_context or _prepare_analysis_context(conn, fetched, project_id)
        knowledge_text = context["knowledge_text"]
        project_context = context["project_context"]
        patterns_text = context["patterns_text"]
        image_desc_text = context["image_desc_text"]
        lang = context["language"]

        # Only the direct test helper calls this function without preloaded
        # context. Its supplied connection remains authoritative.
        if email_type == "project_support" and not analysis_context:
            keywords = re.findall(r"\b\w{4,}\b", body_snippet.lower())[:20]
            matched = search_patterns(conn, keywords, limit=3)
            if matched:
                patterns_text = "\n".join(
                    f"- [{p.category}] {p.resolution_action or ''}" for p in matched
                )

        # Inject current project state for proposed_fields

        from src.email.email_envelope import build_envelope_context

        # Envelope org lookup uses WikiDomainIndex only — avoid cross-thread conn use.
        envelope = build_envelope_context(fetched, project_id=project_id, conn=None)
        user_parts = [
            f"Email type: {email_type}",
            f"Project: {project_id}",
            f"Subject: {fetched.subject or '(no subject)'}",
            f"From: {fetched.sender_email}",
        ]
        if envelope:
            user_parts += ["", envelope]
        user_parts += ["", body_snippet]
        if project_context:
            user_parts += ["", "<current_project>", project_context, "</current_project>"]
        if knowledge_text:
            user_parts += ["", "<knowledge>", knowledge_text, "</knowledge>"]
        if patterns_text:
            user_parts += ["", "<patterns>", patterns_text, "</patterns>"]

        # Attachment filenames + extracted PDF/DOCX text (conformance vs network)
        from src.email.email_envelope import build_attachment_context

        att_block = build_attachment_context(getattr(fetched, "attachments", None))
        if att_block:
            user_parts += ["", att_block]

        # Inject image attachment descriptions if available
        if image_desc_text:
            user_parts += ["", "<image-attachments>", image_desc_text, "</image-attachments>"]

        from src.utils.operator_language import (
            operator_language_instruction,
            summary_action_language_rule,
        )
        system_prompt = load_prompt("routing/reply_analyser.md")
        system_prompt += (
            "\n\n## " + operator_language_instruction(lang)
            + "\n\n## Summary language\n" + summary_action_language_rule(lang)
        )

        prompt = [
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": "\n".join(user_parts),
            },
        ]
        schema = {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "suggested_action": {"type": "string"},
                "confidence": {"type": "number"},
                "proposed_fields": {
                    "type": "object",
                    "description": "Project field changes to propose, or empty object if none",
                },
                "assessment_detail": {
                    "type": "object",
                    "description": (
                        "Optional ProjectStateAssessment-compatible evidence extraction. "
                        "It is not write authorization."
                    ),
                },
                "requested_docs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": [],
                },
                "email_intent": {
                    "type": "string",
                    "enum": [
                        "client_internal_delegation",
                        "client_delivery",
                        "client_request_to_connectivity_acceptance",
                        "client_commitment",
                        "client_fyi",
                        "pm_forward",
                        "pm_handoff_to_external",
                        "ambiguous",
                    ],
                    "description": "Primary ownership intent — classify before proposing ball_with",
                },
            },
            "required": ["summary", "suggested_action", "email_intent"],
        }
        result = llm_call_structured("inbound_reply_analysis", prompt, schema)
        proposed = result.get("proposed_fields") or {}
        # Filter to only the 7 proposable fields
        proposable = {
            "current_milestone", "ball_with", "next_action",
            "project_status", "target_go_live", "sla_limit", "project_tier",
        }
        proposed = {k: v for k, v in proposed.items() if k in proposable and v is not None}
        requested_docs = [
            k for k in (result.get("requested_docs") or [])
            if isinstance(k, str)
        ]
        confidence = float(result.get("confidence", 0.7))
        email_intent = result.get("email_intent", "")
        confidence, proposed = adjust_email_analysis_result(
            body_snippet,
            confidence,
            proposed,
            sender_email=fetched.sender_email,
            email_intent=email_intent,
            has_attachments=bool(getattr(fetched, "attachments", None)),
        )
        return (
            result.get("summary", ""),
            result.get("suggested_action", ""),
            confidence,
            proposed,
            requested_docs,
        )
    except Exception:
        logger.exception("Reply analysis LLM call failed — using fallback")
        subject = fetched.subject or "(no subject)"
        return (
            f"Reply received from {fetched.sender_email}: {subject}",
            f"Review inbound reply from {fetched.sender_email} for project {project_id}.",
            0.5,
            {},
            [],
        )


def _prepare_analysis_context(
    conn: sqlite3.Connection,
    fetched: FetchedEmail,
    project_id: str,
    *,
    email_type: str = "project_update",
) -> dict:
    """Load thread-affine analysis context before starting LLM workers."""
    from src.utils.operator_language import read_response_language
    from src.wiki.retrieval import WikiRetrievalMode, WikiSearchRequest, search_wiki

    body_snippet = (fetched.body or "")[:EMAIL_BODY_SNIPPET_CHARS]
    project_row = conn.execute(
        "SELECT market FROM projects WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    from src.wiki.indexer import format_wiki_hits
    queries = wiki_queries_for_email_body(body_snippet, project_id)
    knowledge_hits = []
    seen_paths = set()
    for index, query in enumerate(queries):
        found = search_wiki(WikiSearchRequest(
            query=query, mode=WikiRetrievalMode.PROJECT_CONTEXT,
            project_id=project_id, market=project_row["market"] if project_row else None,
            top_k=1 if len(queries) > 1 and index == 0 else 3,
        ))
        for hit in found.hits:
            if hit["path"] not in seen_paths:
                seen_paths.add(hit["path"])
                knowledge_hits.append(hit)
    knowledge_text = format_wiki_hits(knowledge_hits, query=" ".join(queries)) if knowledge_hits else ""
    patterns_text = ""
    if email_type == "project_support":
        keywords = re.findall(r"\b\w{4,}\b", body_snippet.lower())[:20]
        matched = search_patterns(conn, keywords, limit=3)
        if matched:
            patterns_text = "\n".join(
                f"- [{p.category}] {p.resolution_action or ''}" for p in matched
            )
    return {
        "knowledge_text": knowledge_text,
        "project_context": _build_project_context(conn, project_id),
        "patterns_text": patterns_text,
        "image_desc_text": _load_image_descriptions(conn, project_id, fetched.email_id),
        "language": read_response_language(conn),
    }


_PROJECT_CONTEXT_FIELDS = [
    "current_milestone", "ball_with", "next_action",
    "project_status", "target_go_live", "sla_limit", "project_tier",
]


def _build_project_context(conn: sqlite3.Connection, project_id: str) -> str:
    """Build a compact text block of current project state for the LLM."""
    try:
        row = conn.execute(
            "SELECT * FROM projects WHERE project_id = ?", (project_id,)
        ).fetchone()
        if not row:
            return ""
        d = dict(row)
        parts = []
        for field in _PROJECT_CONTEXT_FIELDS:
            val = d.get(field)
            if val:
                parts.append(f"{field}: {val}")
        return "\n".join(parts)
    except Exception:
        logger.debug("Could not load project context for %s", project_id, exc_info=True)
        return ""


def _load_image_descriptions(
    conn: sqlite3.Connection,
    project_id: str,
    email_id: str,
) -> str:
    """Load image attachment descriptions from evidence_store.

    Looks for recent attachment-type evidence linked to this project,
    filtering by source_ref containing the email_id.
    Falls back to all recent attachment evidence for the project.
    """
    try:
        rows = conn.execute(
            """SELECT content_summary, source_ref FROM evidence_store
               WHERE project_id = ? AND evidence_type = 'attachment'
               ORDER BY evidence_at DESC LIMIT 10""",
            (project_id,),
        ).fetchall()
        if not rows:
            return ""

        parts = []
        parent_prefix = f"attachment:{email_id}:"
        for row in rows:
            summary = row["content_summary"] or ""
            source = row["source_ref"] or ""
            is_structured = source.startswith("attachment:")
            if is_structured and not source.startswith(parent_prefix):
                continue
            if summary:
                path = source[len(parent_prefix):] if source.startswith(parent_prefix) else source
                label = path.rsplit("/", 1)[-1] if path else "image"
                parts.append(f"[{label}]: {summary}")

        return "\n".join(parts)
    except Exception:
        logger.debug("Could not load image descriptions for %s", project_id, exc_info=True)
        return ""
