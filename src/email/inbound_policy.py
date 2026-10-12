"""
Shared inbound email policy for IMAP and Graph runtimes.
Decides when operator notification is warranted and records audit trail.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from datetime import datetime, time, timedelta, timezone

logger = logging.getLogger(__name__)

from config.defaults import EMAIL_BODY_ANALYSIS_CHARS, EMAIL_BODY_SNIPPET_CHARS, EMAIL_BODY_STORE_CHARS
from config.settings import CONFIG
from src.db.connection import scoped_write
from src.email.inbound_route_decision import decide_inbound_route
from src.email.inbound_work import (
    InboundRouteClaim, ProjectApplicationReceipt, ProjectFieldCommandReceipt,
    ProjectStateCommandReceipt, ProjectLagReviewCommandReceipt, checkpoint_route_step,
)
from src.email.inbound_project_persistence import (
    persist_inbound_project_analysis,
)
from src.repositories.activity_log_repo import insert_activity

from src.telegram.i18n import t
from src.triage.operational import handle_operational_email
from src.triage.support_routing import handle_support_email
from src.triage.support_signals import SUPPORT_SIGNAL_THRESHOLD, count_support_signals
from src.utils.prompt_loader import load_prompt

# Chat ID used for email-triggered agent calls — isolated from the operator's real session.
EMAIL_BACKGROUND_CHAT_ID = 0


def build_email_classifier():
    """Return a callable that classifies inbound email using the LLM."""

    def _classify(
        sender_email: str,
        subject: str,
        body: str,
        projects: list[dict],
        thread_history: list[dict],
        contact_hint: dict | None,
    ) -> dict:
        from src.llm.client import llm_call_structured

        # Build structured project context (include email domain so LLM can reject domain mismatches)
        from src.triage.client_emails import client_email_domains

        def _domain_hint(raw_email: str | None) -> str:
            if not raw_email:
                return ""
            domains = sorted(client_email_domains(raw_email))
            if not domains:
                return ""
            return " (client email domain" + ("s" if len(domains) > 1 else "") + ": " + ", ".join(domains) + ")"

        project_lines = "\n".join(
            "  - {pid}: {name}{domain}".format(
                pid=p["project_id"],
                name=p.get("client_name") or "unknown client",
                domain=_domain_hint(p.get("client_email")),
            )
            for p in projects
        ) or "  (no active projects)"

        # Build structured thread history (per Blueprint Section 17.4)
        if thread_history:
            thread_lines = "\n".join(
                f"  - [{h['processed_at'][:10]}] {h['subject'][:80]} ({h['result']})"
                for h in thread_history
            )
        else:
            thread_lines = "  (no prior emails from this sender)"

        # Build contact hint section
        if contact_hint:
            contact_lines = (
                f"  Known contact: {contact_hint.get('contact_name') or 'unknown'}\n"
                f"  Role: {contact_hint.get('role') or 'unknown'}\n"
                f"  Organisation: {contact_hint.get('organisation') or 'unknown'}\n"
                f"  Resolution confidence: {contact_hint.get('confidence', 0):.2f}"
            )
        else:
            contact_lines = "  Sender is not a known contact"

        prompt = (
            load_prompt("routing/email_classifier.md") + "\n\n"
            "## Active Projects\n"
            f"{project_lines}\n\n"
            "## Prior Thread History (from same sender)\n"
            f"{thread_lines}\n\n"
            "## Contact Hints\n"
            f"{contact_lines}\n\n"
            f"## This Email\n"
            f"From: {sender_email}\n"
            f"Subject: {subject}\n"
            f"Body (first {EMAIL_BODY_ANALYSIS_CHARS} chars):\n{body[:EMAIL_BODY_ANALYSIS_CHARS]}"
        )
        schema = {
            "classification_type": "project_update|new_inquiry|operational|internal|support_issue|project_support|spam|ambiguous",
            "project_id": "project ID string if project_update, else null",
            "confidence": 0.85,
            "reason": "one-line reason",
        }
        return llm_call_structured("triage", [{"role": "user", "content": prompt}], schema, temperature=0.1)

    return _classify


def should_run_inbound_reply_agent(
    event,
    *,
    notification: dict | None,
    has_in_reply_to: bool,
    is_internal_sender: bool,
) -> bool:
    """Whether scheduler should run the legacy inbound-reply agent path.

    ``route_inbound_event`` already auto-processes ``project_update`` emails
    (agent loop + auto-apply) and returns ``None`` on success.  Skip the
    separate ``_process_inbound_reply_via_agent`` call in that case to avoid
    duplicate auto-apply cards and double writes.
    """
    if not has_in_reply_to or not getattr(event, "project_id", None):
        return False
    if is_internal_sender or notification:
        return False
    if getattr(event, "suppressed_reason", None) in {"project_update_auto_processed", "delivery_failure"}:
        return False
    return True


def route_inbound_event(
    conn: sqlite3.Connection | None,
    event,
    body: str,
    *,
    fetched=None,
    post_commit: list[Callable[[], None]] | None = None,
    route_claim: InboundRouteClaim | None = None,
    direct_support: bool = False,
) -> dict | None:
    """Apply operator-notification policy to one classified inbound email.

    When ``conn`` is None the function manages its own short-lived
    ``scoped_write()`` connections so that no DB lock is held during LLM
    calls (which can take 5-45 seconds). A supplied connection remains
    caller-owned: its optional Telegram effects are registered in
    ``post_commit`` and must be dispatched only after commit and after any
    owned writer lease has been released. If no dispatcher is supplied,
    optional cards are not sent.
    """
    from contextlib import contextmanager

    owned_effects: list[Callable[[], None]] = []
    effects = post_commit if conn is not None else owned_effects

    @contextmanager
    def _wconn():
        """Yield the supplied connection or a short-lived owned write scope."""
        if conn is not None:
            # Keep a borrowed Graph connection idle through analysis; open its
            # command transaction only at the first actual write scope.
            if not conn.in_transaction:
                conn.execute("BEGIN")
            yield conn
        else:
            try:
                with scoped_write() as wc:
                    yield wc
            except BaseException:
                owned_effects.clear()
                raise
            # The owned write scope has committed and released its lease.
            callbacks = tuple(owned_effects)
            owned_effects.clear()
            for callback in callbacks:
                try:
                    callback()
                except Exception:
                    logger.exception("Post-commit inbound effect failed for %s", event.email_id)
    event.has_attachments = bool(getattr(fetched, "attachments", ())) if fetched is not None else None
    from src.email.ticket_notice import is_delivery_failure
    decision = decide_inbound_route(
        event,
        body,
        delivery_failure_detector=is_delivery_failure,
        support_signal_counter=count_support_signals,
        support_signal_threshold=SUPPORT_SIGNAL_THRESHOLD,
    )
    classification = decision.classification

    # Keep delivery reports as source/audit records, not cards, digests, or replies.
    if decision.route == "delivery_failure":
        event.should_notify_operator = False
        event.suppressed_reason = "delivery_failure"
        with _wconn() as wc:
            _record_policy_activity(wc, event, action_type="email_notification_suppressed", body=body)
        return None

    if decision.route == "operational":
        with _wconn() as wc:
            handle_operational_email(wc, event, body, commit=False)
            _record_policy_activity(wc, event, action_type="email_notification_suppressed")
        return None

    if decision.route == "internal":
        with _wconn() as wc:
            _record_policy_activity(wc, event, action_type="email_notification_suppressed")
        return None

    if decision.route == "spam":
        with _wconn() as wc:
            _record_policy_activity(wc, event, action_type="email_notification_suppressed")
        return None

    # Support issue routing — create ticket + evidence, queue to digest
    if decision.route == "support":
        from src.db.savepoint import savepoint
        from src.email.support_effects import process_support_effects

        previous_ticket = getattr(event, "ticket_id", None)
        previous_counterparty = getattr(event, "counterparty", None)
        started_caller_transaction = conn is not None and not conn.in_transaction
        try:
            # Releasing the outermost SQLite savepoint would otherwise commit an
            # idle caller connection. Start a caller-owned transaction first.
            if started_caller_transaction:
                conn.execute("BEGIN")
            with _wconn() as wc, savepoint(wc):
                notification = handle_support_email(
                    wc, event, body, route_claim=route_claim,
                    direct_support=direct_support,
                )
                if notification is not None:
                    _record_policy_activity(wc, event, action_type="email_support_ticket_created")
                    from src.email.support_effects import enqueue_support_digest
                    enqueue_support_digest(
                        wc, email_id=event.email_id, route_claim=route_claim,
                        direct_support=direct_support, category="support_ticket",
                        title=f"🔧 {(event.subject or '')[:50]}",
                        body=f"From: {event.sender_email}\nClassification: {classification}",
                        project_id=event.project_id,
                        metadata={"ticket_id": notification.ticket_id},
                    )
        except Exception:
            event.ticket_id = previous_ticket
            event.counterparty = previous_counterparty
            if started_caller_transaction:
                conn.rollback()
            raise
        # Caller-owned scopes are drained by their post-commit entry point or the poller.
        if notification is not None and conn is None:
            try:
                process_support_effects(email_id=event.email_id)
            except Exception:
                logger.exception("Committed support effects deferred for %s", event.email_id)
        return None

    if decision.route == "project_update":
        from src.triage.contact_learning import maybe_enqueue_contact_learning

        from src.email.inbox_fetcher import FetchedEmail

        _fetched = fetched
        if _fetched is None:
            _fetched = FetchedEmail(
                email_id=getattr(event, "email_id", "") or "",
                sender_email=event.sender_email or "",
                subject=event.subject or "",
                body=body,
                received_at=getattr(event, "received_at", "") or "",
                folder="inbox",
                to_emails=list(getattr(event, "to_emails", None) or []),
                cc=getattr(event, "cc", None),
            )
        from src.email.project_truth.shadow_integration import (
            capture_email_project_truth_shadow_job,
            submit_email_project_truth_shadow_fail_open,
        )

        shadow_job = capture_email_project_truth_shadow_job(
            conn,
            event=event,
            body=body,
            fetched=_fetched,
        )
        _processing_succeeded = False
        analysis_summary: str | None = None
        suggested_action: str | None = None
        write_action: str | None = None
        shadow_baseline_fields: dict[str, str] = {}
        shadow_trace_id: str | None = None
        shadow_run_id: str | None = None
        shadow_turn_id: str | None = None
        serve_completed = False
        pending_presentation: tuple[str, dict] | None = None
        from src.email.project_truth.serve import (
            apply_email_project_truth_serve_result,
            email_project_truth_serve_enabled,
            run_email_project_truth_serve,
        )

        completion_persisted = False
        contact_learning_result = None

        def _persist_project_update_completion(wc: sqlite3.Connection) -> dict | None:
            nonlocal pending_presentation
            from src.db.savepoint import savepoint

            contact_learning_result = None
            callbacks_before_completion = len(effects) if effects is not None else 0
            previous_suppression = getattr(event, "suppressed_reason", None)
            previous_write_action = getattr(event, "write_action", None)
            try:
                with savepoint(wc, ensure_transaction=True):
                    try:
                        # A failed optional suggestion must not leave partial writes
                        # inside an otherwise successful project-update command.
                        with savepoint(wc):
                            # Never perform Telegram I/O inside this write scope.
                            contact_learning_result = maybe_enqueue_contact_learning(
                                wc,
                                event,
                                sender_name=getattr(event, "sender_name", None),
                                push_card=False,
                            )
                    except Exception:
                        logger.exception("maybe_enqueue_contact_learning failed for %s", event.project_id)
                    if contact_learning_result and contact_learning_result.get("should_push"):
                        from src.triage.contact_identity_card_outbox import enqueue_contact_identity_card

                        # A successful suggestion and its delivery intent must commit together.
                        # A failed enqueue must not leave a success audit without that intent.
                        enqueue_contact_identity_card(wc, contact_learning_result["suggestion_id"])
                    event.suppressed_reason = "project_update_auto_processed"
                    event.write_action = write_action
                    _record_policy_activity(
                        wc,
                        event,
                        action_type="email_project_update_auto_processed",
                        body=body,
                        analysis_summary=analysis_summary,
                        suggested_action=suggested_action,
                    )
                    from src.email.project_advisory_outbox import enqueue_project_advisory
                    enqueue_project_advisory(
                        wc, email_id=event.email_id, project_id=event.project_id,
                        subject=event.subject or "", body=body,
                    )
                    if pending_presentation is not None:
                        from src.email.inbound_presentation import (
                            prepare_email_info_card, prepare_plain_notification,
                        )
                        from src.email.inbound_presentation_outbox import enqueue_presentation

                        kind, presentation_args = pending_presentation
                        payload = (
                            prepare_email_info_card(wc, event.project_id, **presentation_args)
                            if kind == "info_card" else
                            prepare_plain_notification(presentation_args["result"], event.project_id)
                        )
                        if payload is not None:
                            enqueue_presentation(
                                wc, email_id=event.email_id, project_id=event.project_id,
                                kind=kind, payload=payload,
                            )
                        else:
                            pending_presentation = None
            except BaseException:
                if effects is not None:
                    del effects[callbacks_before_completion:]
                event.suppressed_reason = previous_suppression
                event.write_action = previous_write_action
                raise
            return contact_learning_result

        serve_mode = email_project_truth_serve_enabled()
        if serve_mode and shadow_job is not None:
            from config import defaults

            serve_result = run_email_project_truth_serve(
                shadow_job,
                model_override=(
                    getattr(defaults, "EMAIL_PROJECT_TRUTH_SHADOW_MODEL", "") or None
                ),
                timeout_seconds=int(
                    getattr(defaults, "EMAIL_PROJECT_TRUTH_SHADOW_TIMEOUT_SECONDS", 20)
                ),
            )
            shadow_trace_id = serve_result.trace_id
            shadow_run_id = serve_result.run_id
            if serve_result.completed:
                # A valid B abstention is a completed decision. Never invoke A after
                # extraction succeeds, even if downstream persistence/card routing fails.
                serve_completed = True
                # The B application may run nested action/PCT commands. Give an
                # idle borrowed connection an explicit caller-owned transaction
                # before those commands decide whether they own a commit.
                if conn is not None and not conn.in_transaction:
                    conn.execute("BEGIN")
                from src.db.savepoint import savepoint

                callbacks_before = len(effects) if effects is not None else 0
                try:
                    with _wconn() as wc, savepoint(wc, ensure_transaction=True):
                        if route_claim is not None:
                            if (route_claim.email_id != event.email_id
                                    or serve_result.source_ref != route_claim.email_id):
                                raise ValueError("Project application claim does not match its source")
                            if not checkpoint_route_step(
                                wc, route_claim.email_id, route_claim.token,
                                step="project_update_application", status="started",
                            ):
                                raise RuntimeError("Project application route claim is stale or repeated")
                        application = apply_email_project_truth_serve_result(
                            wc,
                            serve_result,
                            sender_email=event.sender_email,
                            subject=event.subject or "",
                            body=body[:EMAIL_BODY_SNIPPET_CHARS],
                            has_attachments=bool(getattr(_fetched, "attachments", None)),
                            post_commit=effects,
                        )
                        analysis_summary = application.summary or None
                        suggested_action = application.suggested_action or None
                        write_action = application.write_action
                        shadow_baseline_fields = dict(application.proposed_fields)
                        _processing_succeeded = not application.requires_reconciliation
                        if _processing_succeeded:
                            if application.should_push_info_card and application.summary:
                                pending_presentation = ("info_card", {
                                    "summary": application.summary,
                                    "suggested_action": application.suggested_action,
                                    "sender_email": event.sender_email,
                                    "subject": event.subject, "body": body,
                                    "serve_result": serve_result,
                                    "write_action": application.write_action,
                                    "fetched": _fetched,
                                })
                            contact_learning_result = _persist_project_update_completion(wc)
                            completion_persisted = True
                        if route_claim is not None:
                            command = application.state_command
                            if command is not None and command.status == "committed":
                                raise RuntimeError("Independent state committed outside application transaction")
                            state_receipt = None if command is None else ProjectStateCommandReceipt(
                                project_id=command.project_id, source_ref=command.source_ref,
                                source_type=command.source_type, write_action=command.write_action,
                                status=("applied_in_application_transaction"
                                        if command.status == "persisted_in_caller_transaction"
                                        else command.status),
                                proposed_fields=dict(command.proposed_fields),
                                evidence_id=command.evidence_id, update_id=command.update_id,
                                review_id=command.review_id,
                            )
                            field_command = application.field_command
                            if field_command is not None and field_command.status not in {
                                "persisted_in_caller_transaction", "not_applied", "incomplete",
                            }:
                                raise RuntimeError("Unexpected B field command transaction ownership")
                            field_receipt = None if field_command is None else ProjectFieldCommandReceipt(
                                project_id=field_command.project_id,
                                source_ref=field_command.source_ref,
                                source_type=field_command.source_type,
                                write_action=field_command.write_action,
                                status=("applied_in_application_transaction"
                                        if field_command.status == "persisted_in_caller_transaction"
                                        else field_command.status),
                                proposed_fields=dict(field_command.proposed_fields),
                                evidence_id=field_command.evidence_id,
                                update_id=field_command.update_id,
                                review_id=field_command.review_id,
                            )
                            lag_command = application.lag_review_command
                            if lag_command is not None and lag_command.status == "committed":
                                raise RuntimeError("Lag preview committed outside application transaction")
                            lag_receipt = None if lag_command is None else ProjectLagReviewCommandReceipt(
                                project_id=lag_command.project_id, source_ref=lag_command.source_ref,
                                source_type=lag_command.source_type,
                                preview_result=lag_command.preview_result,
                                status=("proposed_in_application_transaction"
                                        if lag_command.status == "persisted_in_caller_transaction"
                                        else lag_command.status),
                                proposed_update_id=lag_command.proposed_update_id,
                                proposed_fields=dict(lag_command.proposed_fields),
                            )
                            receipt = ProjectApplicationReceipt(
                                project_id=serve_result.project_id,
                                source_ref=serve_result.source_ref,
                                status=application.status,
                                write_action=application.write_action,
                                state_write_action=application.state_write_action,
                                state_command=state_receipt,
                                pct_command=application.pct_command,
                                field_command=field_receipt,
                                lag_review_command=lag_receipt,
                                action_command=application.action_command,
                                action_review_id=application.action_review_id,
                                requires_reconciliation=application.requires_reconciliation,
                            )
                            if not checkpoint_route_step(
                                wc, route_claim.email_id, route_claim.token,
                                step="project_update_application",
                                status=("incomplete" if application.requires_reconciliation
                                        else "completed"),
                                command_receipt=receipt,
                            ):
                                raise RuntimeError("Project application receipt could not be committed")
                except BaseException:
                    if effects is not None:
                        del effects[callbacks_before:]
                    raise
                if not _processing_succeeded:
                    logger.error(
                        "Email Project Truth B application incomplete for %s (%s; %s; %s)",
                        event.project_id,
                        application.error_class or "unknown",
                        application.write_action,
                        application.state_write_action,
                    )
            else:
                logger.warning(
                    "Email Project Truth B execution failed for %s (%s); falling back to A",
                    event.project_id,
                    serve_result.error_class or "unknown",
                )
        elif serve_mode:
            logger.warning(
                "Email Project Truth B could not capture a project snapshot for %s; falling back to A",
                event.project_id,
            )
        if not serve_completed:
            from src.email.email_envelope import build_inbound_analysis_text

            user_text = build_inbound_analysis_text(
                project_id=event.project_id,
                sender_email=event.sender_email,
                subject=event.subject or "",
                body=body[:EMAIL_BODY_STORE_CHARS],
                fetched=_fetched,
                conn=conn,
            )
            try:
                from src.email.agent_entry import run_project_update_analysis

                agent_analysis = run_project_update_analysis(
                    project_id=event.project_id,
                    email_id=event.email_id,
                    user_text=user_text,
                    chat_id=EMAIL_BACKGROUND_CHAT_ID,
                )
                result = agent_analysis.reply
                shadow_trace_id = agent_analysis.trace_id
                shadow_run_id = agent_analysis.run_id
                shadow_turn_id = agent_analysis.turn_id
                stored = agent_analysis.stored
                followup_intents = tuple(getattr(agent_analysis, "followup_intents", ()))
                if stored:
                    analysis_summary = (stored.get("summary") or "").strip() or None
                    suggested_action = (stored.get("suggested_action") or "").strip() or None
                    from src.db.savepoint import savepoint

                    # The successful A application and its completion intents
                    # form one command. A later advisory/presentation failure must
                    # not leave a committed field/review write or run callbacks.
                    if conn is not None and not conn.in_transaction:
                        conn.execute("BEGIN")
                    callbacks_before = len(effects) if effects is not None else 0
                    previous_suppression = getattr(event, "suppressed_reason", None)
                    previous_write_action = getattr(event, "write_action", None)
                    try:
                        with _wconn() as wc, savepoint(wc):
                            if route_claim is not None:
                                if (route_claim.email_id != event.email_id
                                        or _fetched.email_id != route_claim.email_id):
                                    raise ValueError("Project application claim does not match its source")
                                if not checkpoint_route_step(
                                    wc, route_claim.email_id, route_claim.token,
                                    step="project_update_application", status="started",
                                ):
                                    raise RuntimeError("Project application route claim is stale or repeated")
                            application = persist_inbound_project_analysis(
                                wc, event=event, fetched=_fetched, body=body,
                                stored=stored, logger=logger, post_commit=effects,
                                **({"require_command_identity": True} if route_claim is not None else {}),
                            )
                            write_action = application.write_action
                            from src.email.analysis_presentation_decision import decide_analysis_presentation

                            presentation = decide_analysis_presentation(
                                write_action=write_action, stored=application.stored, reply=result,
                            )
                            followup_commands = ()
                            if presentation.processing_succeeded:
                                from src.email.analysis_execution import persist_followup_intents
                                followup_commands = persist_followup_intents(
                                    wc, followup_intents,
                                    project_id=event.project_id, source_ref=event.email_id,
                                )
                            elif followup_intents:
                                from src.email.analysis_execution import unapplied_followup_intents
                                followup_commands = unapplied_followup_intents(
                                    followup_intents, project_id=event.project_id,
                                    source_ref=event.email_id, reason="application_incomplete",
                                )
                            shadow_baseline_fields = application.baseline_fields
                            stored = application.stored
                            if presentation.presentation_kind == "info_card":
                                pending_presentation = ("info_card", {
                                    "summary": stored.get("summary") or "",
                                    "suggested_action": stored.get("suggested_action") or "",
                                    "sender_email": event.sender_email,
                                    "subject": event.subject, "body": body,
                                    "fetched": _fetched, "write_action": write_action,
                                })
                            elif presentation.presentation_kind == "plain":
                                pending_presentation = ("plain", {"result": presentation.reply})
                            _processing_succeeded = presentation.processing_succeeded
                            if _processing_succeeded:
                                contact_learning_result = _persist_project_update_completion(wc)
                                completion_persisted = True
                            if route_claim is not None:
                                # Re-correlate the exported review identity at the A
                                # application boundary before certifying its checkpoint.
                                # A typed/model-copied object is not physical DB evidence.
                                if application.field_review_command is not None:
                                    from src.email.inbound_project_persistence import validate_field_review_command

                                    validate_field_review_command(
                                        wc, application.field_review_command,
                                        outcome=application.field_review_command.write_action,
                                        project_id=event.project_id, source_ref=event.email_id,
                                        evidence_ref=application.field_review_command.evidence_id,
                                    )
                                if application.field_effect_command is not None:
                                    from src.email.inbound_field_effect_identity import validate_field_effect_command

                                    validate_field_effect_command(
                                        wc, application.field_effect_command,
                                        outcome=application.field_effect_command.write_action,
                                        project_id=event.project_id, source_ref=event.email_id,
                                        evidence_ref=application.field_effect_command.evidence_id,
                                    )
                                receipt = ProjectApplicationReceipt(
                                    project_id=event.project_id,
                                    source_ref=event.email_id,
                                    status="completed" if _processing_succeeded else "failed",
                                    write_action=write_action,
                                    pct_command=application.pct_command,
                                    field_review_command=application.field_review_command,
                                    field_effect_command=application.field_effect_command,
                                    followup_commands=list(followup_commands),
                                    requires_reconciliation=not _processing_succeeded,
                                )
                                if not checkpoint_route_step(
                                    wc, route_claim.email_id, route_claim.token,
                                    step="project_update_application",
                                    status="completed" if _processing_succeeded else "incomplete",
                                    command_receipt=receipt,
                                ):
                                    raise RuntimeError("Project application receipt could not be committed")
                    except BaseException:
                        if effects is not None:
                            del effects[callbacks_before:]
                        event.suppressed_reason = previous_suppression
                        event.write_action = previous_write_action
                        _processing_succeeded = False
                        completion_persisted = False
                        pending_presentation = None
                        raise
                else:
                    from src.email.analysis_presentation_decision import decide_analysis_presentation

                    presentation = decide_analysis_presentation(
                        write_action=write_action, stored=stored, reply=result,
                    )
                    if presentation.presentation_kind == "plain":
                        pending_presentation = ("plain", {"result": presentation.reply})
                    _processing_succeeded = presentation.processing_succeeded
                    if followup_intents:
                        # No stored report can authorize these staged writes. Persist
                        # their unapplied outcome under the claim, never auto-replay.
                        from src.db.savepoint import savepoint

                        # The missing report cannot authorize success, even if
                        # its negative receipt SQL or owner COMMIT fails.
                        _processing_succeeded = False
                        pending_presentation = None
                        with _wconn() as wc, savepoint(wc, ensure_transaction=True):
                            if route_claim is not None:
                                if (route_claim.email_id != event.email_id
                                        or _fetched.email_id != route_claim.email_id):
                                    raise ValueError("Project application claim does not match its source")
                                if not checkpoint_route_step(
                                    wc, route_claim.email_id, route_claim.token,
                                    step="project_update_application", status="started",
                                ):
                                    raise RuntimeError("Project application route claim is stale or repeated")
                            from src.email.analysis_execution import unapplied_followup_intents
                            commands = unapplied_followup_intents(
                                followup_intents, project_id=event.project_id,
                                source_ref=event.email_id, reason="missing_stored_analysis",
                            )
                            if route_claim is not None:
                                receipt = ProjectApplicationReceipt(
                                    project_id=event.project_id, source_ref=event.email_id,
                                    status="failed", write_action="analysis_no_field_report",
                                    followup_commands=commands, requires_reconciliation=True,
                                )
                                if not checkpoint_route_step(
                                    wc, route_claim.email_id, route_claim.token,
                                    step="project_update_application", status="incomplete",
                                    command_receipt=receipt,
                                ):
                                    raise RuntimeError("Project application receipt could not be committed")
            except Exception:
                # The agent and later phases can fail after committed effects.
                # Replaying a three-field fallback could create a second write.
                logger.exception(
                    "Email project update incomplete for %s; reconciliation required without fallback",
                    event.project_id,
                )

        if not serve_mode:
            submit_email_project_truth_shadow_fail_open(
                shadow_job,
                baseline_status="completed" if _processing_succeeded else "failed",
                baseline_fields=shadow_baseline_fields,
                baseline_summary=analysis_summary,
                baseline_suggested_action=suggested_action,
                trace_id=shadow_trace_id,
                run_id=shadow_run_id,
                turn_id=shadow_turn_id,
            )

        if not _processing_succeeded:
            event.should_notify_operator = True
            from src.email.inbound_presentation import project_update_failure_notification

            notification = project_update_failure_notification(event)
            if serve_completed:
                if application.write_action.startswith("email_action_bundle_"):
                    notification["body"] += (
                        "\n\nThe email action plan was applied or already present, but "
                        "independent project-state processing is incomplete. Verify "
                        "both effects before reconciling; do not replay this email."
                    )
                elif application.write_action == "action_review_persistence_failed":
                    notification["body"] += (
                        "\n\nThe action review could not be saved. Verify any "
                        "independent project-state effects before reconciling; "
                        "do not replay this email."
                    )
                else:
                    notification["body"] += (
                        "\n\nProcessing may have partially committed. Verify "
                        "the recorded effects before reconciling; do not replay this email."
                    )
            # Application failures may follow committed writes; the poller must not
            # turn an operator alert into a successful route receipt.
            notification["route_requires_reconciliation"] = True
            with _wconn() as wc:
                _record_policy_activity(wc, event, action_type="email_operator_notification_queued")
            return notification

        if not completion_persisted:
            from src.db.savepoint import savepoint

            previous_suppression = getattr(event, "suppressed_reason", None)
            previous_write_action = getattr(event, "write_action", None)
            try:
                with _wconn() as wc, savepoint(wc, ensure_transaction=True):
                    if route_claim is not None:
                        if (route_claim.email_id != event.email_id
                                or _fetched.email_id != route_claim.email_id):
                            raise ValueError("Project application claim does not match its source")
                        if not checkpoint_route_step(
                            wc, route_claim.email_id, route_claim.token,
                            step="project_update_application", status="started",
                        ):
                            raise RuntimeError("Project application route claim is stale or repeated")
                    contact_learning_result = _persist_project_update_completion(wc)
                    if route_claim is not None:
                        receipt = ProjectApplicationReceipt(
                            project_id=event.project_id,
                            source_ref=event.email_id,
                            status="completed",
                            write_action="analysis_no_field_report",
                            requires_reconciliation=False,
                        )
                        if not checkpoint_route_step(
                            wc, route_claim.email_id, route_claim.token,
                            step="project_update_application", status="completed",
                            command_receipt=receipt,
                        ):
                            raise RuntimeError("Project application receipt could not be committed")
            except BaseException:
                event.suppressed_reason = previous_suppression
                event.write_action = previous_write_action
                raise
        # The intent is committed with the success audit; only a committed row
        # can be claimed for network I/O. A lost callback is recovered by the
        # periodic worker, while a claimed send is never blindly replayed.
        if pending_presentation is not None:
            def _deliver_committed_presentation() -> None:
                from src.email.inbound_presentation_outbox import deliver_presentation
                deliver_presentation(event.email_id)

            if conn is None:
                try:
                    _deliver_committed_presentation()
                except Exception:
                    logger.exception("Committed email presentation failed for %s", event.email_id)
            elif post_commit is not None:
                post_commit.append(_deliver_committed_presentation)
            else:
                logger.debug("Email presentation requires a post-commit dispatcher for %s", event.email_id)

        def _deliver_optional_cards() -> None:
            if contact_learning_result and contact_learning_result.get("should_push"):
                try:
                    from src.triage.contact_identity_card_outbox import deliver_contact_identity_card
                    deliver_contact_identity_card(contact_learning_result["suggestion_id"])
                except Exception:
                    logger.exception("Committed contact identity card dispatch failed for %s", event.project_id)
            from src.email.project_advisory_outbox import deliver_project_advisory
            try:
                deliver_project_advisory(event.email_id)
            except Exception:
                logger.exception("Project advisory dispatch failed for %s", event.email_id)

        if conn is None:
            _deliver_optional_cards()
        elif post_commit is not None:
            post_commit.append(_deliver_optional_cards)
        else:
            # Caller-owned transactions cannot deliver before their owner commits.
            logger.debug("Optional project cards require a post-commit dispatcher for %s", event.email_id)
        return None

    if decision.route == "operator_review":
        from src.email.inbound_operator_review import OperatorReviewPorts, route_operator_review
        from src.email.triage_conversation import group_triage_notification

        return route_operator_review(
            event, body, classification=classification,
            ports=OperatorReviewPorts(
                write_scope=_wconn,
                already_queued=_operator_notification_already_sent,
                record_activity=_record_policy_activity,
                window_open=_is_operator_notification_window_open,
                build_notification=_build_notification,
                translate=t,
                group_notification=group_triage_notification,
            ),
        )

    with _wconn() as wc:
        _record_policy_activity(wc, event, action_type="email_notification_suppressed")
    return None


def _build_notification(event, *, title_key: str, extra_lines: list[str] | None = None) -> dict:
    from src.telegram.operator_messages import en
    lines = [f"{'From' if en() else '寄件人'}：{event.sender_email}",
             f"{'Subject' if en() else '主旨'}：{event.subject}", "",
             "Please confirm which project this email belongs to; it has not been assigned by this notice."
             if en() else "請確認這封郵件屬於哪個專案；這則通知尚未替你完成指派。"]
    if event.reason:
        lines.append(("Matching context: " if en() else "判斷依據：") + event.reason[:500])
    if extra_lines:
        lines.extend(line for line in extra_lines if line)
    return {
        "profile": "B",
        "title": t(title_key).format(subject=event.subject[:50]),
        "body": "\n".join(lines),
        "project_id": event.project_id,
    }


def _operator_notification_already_sent(conn: sqlite3.Connection, email_id: str) -> bool:
    """Return True if an operator-facing card or digest was already queued."""
    row = conn.execute(
        """SELECT 1 FROM activity_log
           WHERE object_ref = ?
             AND action_type IN (
                 'email_operator_notification_queued',
                 'email_deferred_renotification',
                 'email_queued_to_digest'
             )
           LIMIT 1""",
        (email_id,),
    ).fetchone()
    return row is not None


def _body_sum_up(body: str | None, *, max_len: int = 180) -> str:
    """First readable lines of an email body for activity timeline display."""
    if not body:
        return ""
    # Drop pure greeting-only openers from the sum-up (keep substance)
    _GREETING_ONLY = {
        "hi", "hello", "hi operator", "hi team", "hi all", "dear operator",
        "dear all", "dear team", "thanks", "thank you", "regards",
    }
    lines: list[str] = []
    for raw in body.replace("\r\n", "\n").split("\n"):
        line = raw.strip().rstrip(",")
        if not line:
            continue  # blank lines are normal between greeting and body
        low = line.lower()
        # Corporate disclaimer banners / external-mail warnings
        if (
            low.startswith("external email")
            or "please do not click any link" in low
            or "除非您信任" in line
            or "外部郵件" in line
            or "外部邮件" in line
        ):
            continue
        # Skip quoted reply noise / signatures / thread headers
        if line.startswith(">"):
            if lines:
                break
            continue
        if low.startswith(("on ", "from:", "sent:", "to:", "cc:", "subject:", "date:")):
            if lines:
                break
            continue
        if low.startswith("regards") or low in ("thanks", "thank you", "best regards", "cheers"):
            if lines:
                break
            continue
        if line in ("--", "—", "___") or line.startswith("-----"):
            break
        # Skip standalone greetings; keep "Hi team, please …" substance lines
        if low.rstrip("!.").strip() in _GREETING_ONLY or (
            low.startswith("hi ") and len(line) < 24
        ):
            continue
        lines.append(line)
        joined_len = sum(len(x) + 1 for x in lines)
        if joined_len >= max_len:
            break
    text = " ".join(lines).strip()
    if len(text) > max_len:
        text = text[: max_len - 1].rstrip() + "…"
    return text


def _format_email_activity_summary(
    event,
    *,
    body: str | None = None,
    analysis_summary: str | None = None,
    suggested_action: str | None = None,
) -> str:
    """Human-readable project-timeline summary: sender, subject, sum-up (not subject alone)."""
    subject = (getattr(event, "subject", None) or "").strip() or "(no subject)"
    sender = (getattr(event, "sender_email", None) or "").strip()
    classification = (getattr(event, "classification_type", None) or "email").strip()

    head = f"{classification} · From {sender}" if sender else classification
    lines = [f"{head}", subject[:140]]

    sum_up = (analysis_summary or "").strip()
    if not sum_up:
        reason = (getattr(event, "reason", None) or "").strip()
        # Skip low-value triage reasons that are not content
        if reason and reason.lower() not in {
            "thread_id match",
            "project_update_auto_processed",
            "domain match",
        }:
            sum_up = reason
    if not sum_up:
        sum_up = _body_sum_up(body)
    if sum_up:
        lines.append(sum_up[:200])
    action = (suggested_action or "").strip()
    if action:
        lines.append(f"Next: {action[:120]}")
    return "\n".join(lines)[:450]


def _record_policy_activity(
    conn: sqlite3.Connection,
    event,
    *,
    action_type: str,
    body: str | None = None,
    analysis_summary: str | None = None,
    suggested_action: str | None = None,
) -> None:
    sum_up = (analysis_summary or "").strip() or _body_sum_up(body)
    metadata = {
        "classification_type": _safe_json_value(getattr(event, "classification_type", None)),
        "confidence": _safe_json_value(getattr(event, "confidence", None)),
        "triage_stage_reached": _safe_json_value(getattr(event, "triage_stage_reached", None)),
        "requires_operator_triage": _safe_json_value(getattr(event, "requires_operator_triage", None)),
        "should_notify_operator": _safe_json_value(getattr(event, "should_notify_operator", None)),
        "notification_reason": _safe_json_value(getattr(event, "notification_reason", None)),
        "suppressed_reason": _safe_json_value(getattr(event, "suppressed_reason", None)),
        "llm_failed": _safe_json_value(getattr(event, "llm_failed", None)),
        "sender_email": _safe_json_value(getattr(event, "sender_email", None)),
        "subject": _safe_json_value(getattr(event, "subject", None)),
        "analysis_summary": _safe_json_value(sum_up[:300] if sum_up else None),
        "suggested_action": _safe_json_value(
            (suggested_action or "").strip()[:200] or None
        ),
        "write_action": _safe_json_value(getattr(event, "write_action", None)),
    }
    insert_activity(
        conn,
        actor="system",
        action_type=action_type,
        project_id=event.project_id,
        surface="email",
        object_ref=event.email_id,
        summary=_format_email_activity_summary(
            event,
            body=body,
            analysis_summary=analysis_summary,
            suggested_action=suggested_action,
        ),
        metadata_json=json.dumps(metadata, ensure_ascii=False),
    )
    # The enclosing route/connection scope owns commit or rollback.


def _safe_json_value(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _is_operator_notification_window_open(now: datetime | None = None) -> bool:
    if now is None:
        hkt = timezone(timedelta(hours=8))
        now = datetime.now(hkt)

    if now.weekday() >= 5:
        return False

    start = _parse_clock_time(CONFIG.work_hours_start, default="08:30")
    end = _parse_clock_time(CONFIG.work_hours_end, default="18:00")
    current = now.timetz().replace(tzinfo=None)
    return start <= current <= end


def _parse_clock_time(value: str, *, default: str) -> time:
    try:
        hour_str, minute_str = (value or default).split(":", 1)
        return time(hour=int(hour_str), minute=int(minute_str))
    except (TypeError, ValueError):
        hour_str, minute_str = default.split(":", 1)
        return time(hour=int(hour_str), minute=int(minute_str))


def _extract_fields_from_summary(
    email_summary: dict,
    *,
    current_milestone: str | None = None,
) -> dict:
    """Extract writable project fields from an email summary dict.

    Maps the structured email summary to project field updates.
    Returns an empty dict when no actionable field changes are found.
    """
    fields: dict = {}
    blocking_side = (email_summary.get("blocking_side") or "").strip()
    if blocking_side in ("Internal", "External"):
        fields["ball_with"] = blocking_side
    action_requested = (email_summary.get("action_requested") or "").strip()
    if action_requested:
        fields["next_action"] = action_requested[:500]
    summary_text = (email_summary.get("summary") or "").strip()
    if summary_text:
        fields["notes"] = summary_text[:EMAIL_BODY_SNIPPET_CHARS]
    # The fallback summary lacks evidence-bearing facts/citations, so it may
    # suggest non-milestone fields only. Milestone decisions require the
    # versioned contract evaluator in the structured primary path.
    return fields


def _maybe_push_project_advisory(
    conn: sqlite3.Connection | None, event, body: str,
) -> None:
    """Analyze committed project state and optionally push a Profile O card.

    The legacy connection argument remains for call compatibility; a supplied
    caller connection is never a safe delivery boundary. Pass None only after
    the caller commits and releases any owned writer lease.
    """
    if not event.project_id:
        return
    if conn is not None:
        logger.debug("Advisory needs post-commit dispatch for %s", event.project_id)
        return
    try:
        from src.email.project_advisory_outbox import analyze_project_advisory
        from src.telegram.advisory_push import push_project_advisory_card_sync

        advisory = analyze_project_advisory(
            event.project_id, event.subject or "", body[:EMAIL_BODY_SNIPPET_CHARS],
        )
        if advisory.confidence >= 0.7 and advisory.next_steps:
            push_project_advisory_card_sync(
                None,
                advisory=advisory,
                project_id=event.project_id,
                event_summary=(event.subject or "")[:200],
            )
    except Exception:
        logger.exception("Optional project advisory failed for %s", event.project_id)


def _get_project_milestone_and_ball_with(
    conn: sqlite3.Connection | None, project_id: str,
) -> tuple[str, str | None]:
    """Get current milestone and ball_with for a project."""
    try:
        from src.db.connection import scoped_read
        from src.repositories.project_repo import get_project

        if conn is not None:
            project = get_project(conn, project_id)
        else:
            with scoped_read() as rconn:
                project = get_project(rconn, project_id)
        if project:
            ms = project.current_milestone
            ms_val = ms.value if hasattr(ms, "value") else str(ms) if ms else ""
            bw = project.ball_with
            bw_val = bw.value if hasattr(bw, "value") else str(bw) if bw else None
            return ms_val, bw_val
    except Exception:
        pass
    return "", None


def _get_project_milestone(conn: sqlite3.Connection | None, project_id: str) -> str:
    """Get current milestone for a project. Returns empty string on failure."""
    ms, _ = _get_project_milestone_and_ball_with(conn, project_id)
    return ms


def _is_raw_tool_dump(text: str) -> bool:
    from src.email.inbound_presentation import _is_raw_tool_dump as detect

    return detect(text)


def _format_raw_email_dump(raw: str, project_id: str) -> str | None:
    from src.email.inbound_presentation import _format_raw_email_dump as format_dump

    return format_dump(raw, project_id)


def _push_email_info_card(
    project_id: str,
    *,
    summary: str,
    suggested_action: str = "",
    sender_email: str | None = None,
    subject: str | None = None,
    body: str = "",
    serve_result=None,
    write_action: str | None = "skipped",
    fetched=None,
) -> None:
    """Compatibility entry point for policy and existing call-time patches."""
    from src.email.inbound_presentation import push_email_info_card

    push_email_info_card(
        project_id, summary=summary, suggested_action=suggested_action,
        sender_email=sender_email, subject=subject, body=body,
        serve_result=serve_result, write_action=write_action, fetched=fetched,
        write_scope=scoped_write,
    )


def _send_email_update_notification(result: str, project_id: str) -> None:
    """Compatibility entry point for policy and existing call-time patches."""
    from src.email.inbound_presentation import send_email_update_notification

    send_email_update_notification(
        result, project_id, raw_detector=_is_raw_tool_dump,
        raw_formatter=_format_raw_email_dump,
    )


def check_deferred_email_notifications(conn: sqlite3.Connection) -> list[dict]:
    """Re-check emails suppressed due to outside_operator_hours.

    Called by the deferred-check scheduler job.  Returns a list of
    notification dicts for items that should now be sent because the
    operator notification window has opened.
    """
    if not _is_operator_notification_window_open():
        return []

    rows = conn.execute(
        """SELECT source.id, source.object_ref, source.metadata_json, source.project_id
           FROM activity_log AS source
           WHERE source.action_type = 'email_notification_suppressed'
             AND source.metadata_json LIKE '%outside_operator_hours%'
             AND NOT EXISTS (
                 SELECT 1 FROM activity_log AS receipt
                 WHERE receipt.action_type = 'email_deferred_renotification'
                   AND CASE WHEN json_valid(receipt.metadata_json) THEN
                       CAST(json_extract(receipt.metadata_json, '$.renotify_source_id') AS INTEGER)
                   END = source.id
             )
           ORDER BY source.id""",
    ).fetchall()

    notifications: list[dict] = []
    for row in rows:
        d = dict(row)
        try:
            meta = json.loads(d["metadata_json"])
        except (json.JSONDecodeError, TypeError):
            continue

        classification = meta.get("classification_type", "ambiguous")
        title_key = "policy.new_email_inquiry" if classification == "new_inquiry" else "policy.email_triage"
        notif = {
            "profile": "B",
            "title": t(title_key).format(subject=(meta.get('subject') or '')[:50]),
            "body": (
                f"(Deferred — originally suppressed outside operator hours)\n"
                f"From: {meta.get('sender_email', '?')}\n"
                f"Classification: {classification}"
            ),
            "project_id": d.get("project_id"),
        }
        notifications.append(notif)

        insert_activity(
            conn,
            actor="system",
            action_type="email_deferred_renotification",
            surface="email",
            object_ref=d["object_ref"],
            summary=f"Re-notification for suppressed email: {(meta.get('subject') or '')[:80]}",
            metadata_json=json.dumps({"renotify_source_id": d["id"]}, ensure_ascii=False),
        )

    # The caller must commit these receipts with the corresponding digest rows.
    # Committing here would suppress a retry if a later queue insert fails.
    return notifications
