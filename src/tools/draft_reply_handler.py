"""Focused implementation of the draft_reply tool."""

from __future__ import annotations

import logging
import re
import sqlite3
from typing import Any, Callable

from src.email.draft_grounding import (
    PM_UAT_CANONICAL_PATH,
    attach_docs_to_draft,
    load_approved_draft_wiki,
    pm_uat_correction,
    pm_uat_draft_issues,
    resolve_draft_doc_keys,
)
from src.email.draft_fidelity_audit import (
    _connection_is_query_only,
    clarification_result,
    draft_write_connection,
    fidelity_failure_result,
    record_draft_fidelity_event,
)

logger = logging.getLogger(__name__)

CreateDraft = Callable[..., str]


def _normalize_cc(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple, set)):
        items = [str(item).strip() for item in value if str(item).strip()]
        return ", ".join(items) or None
    return str(value).strip() or None


def _standalone_subject(subject: str, project_id: str) -> str:
    cleaned = re.sub(
        r"^(?:(?:re|fw|fwd)\s*:\s*)+",
        "",
        (subject or "").strip(),
        flags=re.IGNORECASE,
    )
    cleaned = cleaned or "Operational request"
    tag = f"[{project_id}]"
    if tag.casefold() not in cleaned.casefold():
        cleaned = f"{tag} {cleaned}"
    return cleaned


def _handle_adhoc_draft(
    conn: sqlite3.Connection,
    arguments: dict[str, Any],
    *,
    execution_context: dict[str, Any],
) -> dict[str, Any]:
    from src.communications.compose_service import (
        persist_prepared_email_draft,
        prepare_email_draft,
    )
    from src.email.instruction_fidelity import (
        build_directive_spec,
        resolve_trusted_internal_recipient,
        retry_instruction,
        validate_draft_fidelity,
    )

    operator_text = str(execution_context.get("operator_request_text") or "").strip()
    background_context = str(arguments.get("context") or "").strip()
    to_email = arguments.get("to_email")
    if not to_email and not arguments.get("contact_id"):
        to_email = resolve_trusted_internal_recipient(operator_text)

    spec = build_directive_spec(operator_text, background_context=background_context)
    attempts: list[dict[str, Any]] = []
    if spec.requires_clarification:
        return clarification_result(
            conn,
            spec=spec,
            attempts=attempts,
            thread_mode="standalone",
            to_email=to_email,
            execution_context=execution_context,
        )

    prepared = prepare_email_draft(
        conn,
        context=background_context,
        to_email=to_email,
        contact_id=arguments.get("contact_id"),
        contact_name=arguments.get("contact_name"),
        subject=arguments.get("subject"),
        body=arguments.get("body"),
        operator_directive=operator_text or None,
    )
    issues = validate_draft_fidelity(prepared.body, spec)
    attempts.append({"attempt": 1, "fidelity_issues": issues, "content_issues": []})
    if issues:
        prepared = prepare_email_draft(
            conn,
            context=background_context,
            to_email=to_email,
            contact_id=arguments.get("contact_id"),
            contact_name=arguments.get("contact_name"),
            subject=arguments.get("subject"),
            operator_directive=operator_text or None,
            validation_feedback=retry_instruction(spec, issues),
        )
        issues = validate_draft_fidelity(prepared.body, spec)
        attempts.append({"attempt": 2, "fidelity_issues": issues, "content_issues": []})
        if issues:
            return fidelity_failure_result(
                conn,
                spec=spec,
                attempts=attempts,
                thread_mode="standalone",
                to_email=prepared.to_email,
                execution_context=execution_context,
                issues=issues,
            )

    composed = persist_prepared_email_draft(
        conn,
        prepared,
        cc=_normalize_cc(arguments.get("cc")),
        purpose="internal_follow_up",
        write_connection_factory=execution_context.get("write_connection_factory"),
    )
    record_draft_fidelity_event(
        conn,
        status="ok",
        spec=spec,
        attempts=attempts,
        thread_mode="standalone",
        to_email=composed.to_email,
        execution_context=execution_context,
        draft_id=composed.draft_id,
    )
    return {
        "status": "created",
        "draft_id": composed.draft_id,
        "subject": composed.subject,
        "body": composed.body,
        "to_email": composed.to_email,
        "project_id": None,
        "adhoc": True,
        "thread_mode": "standalone",
        "message": "Ad-hoc draft saved for review. The email has not been sent.",
    }


def handle_draft_reply(
    conn: sqlite3.Connection,
    arguments: dict[str, Any],
    *,
    execution_context: dict[str, Any] | None,
    create_draft_fn: CreateDraft,
) -> dict[str, Any]:
    """Draft an email while preserving trusted operator action directives."""
    from src.email.draft_generator import generate_email_draft
    from src.email.envelope import resolve_draft_envelope
    from src.email.instruction_fidelity import (
        build_directive_spec,
        internal_recipient_was_requested,
        load_case_facts,
        resolve_trusted_internal_recipient,
        retry_instruction,
        validate_draft_fidelity,
    )
    from src.llm.client import LLMParseFailure
    from src.email.attachment_recovery import AttachmentRecoveryError

    context = dict(execution_context or {})
    project_id = str(arguments.get("project_id") or "").strip()
    try:
        if not project_id:
            return _handle_adhoc_draft(conn, arguments, execution_context=context)

        operator_text = str(context.get("operator_request_text") or "").strip()
        background_context = str(
            arguments.get("context") or "General follow-up"
        ).strip()
        attempts: list[dict[str, Any]] = []

        wiki_paths = arguments.get("wiki_paths") or []
        approved_knowledge = load_approved_draft_wiki(wiki_paths)
        context_hints = {
            "background_context": background_context,
            "approved_knowledge": approved_knowledge,
        }

        recipient_mode = arguments.get("recipient_mode") or "thread_sender"
        recipient_override: dict[str, Any] | None = None
        to_email_arg = arguments.get("to_email")
        contact_id = arguments.get("contact_id")
        resolved_team_recipient = resolve_trusted_internal_recipient(operator_text)
        trusted_team_recipient = None
        if not contact_id and resolved_team_recipient:
            if not to_email_arg:
                to_email_arg = resolved_team_recipient
                trusted_team_recipient = resolved_team_recipient
            elif str(to_email_arg).strip().casefold() == resolved_team_recipient:
                trusted_team_recipient = resolved_team_recipient
        if recipient_mode == "explicit" and not (to_email_arg or contact_id):
            return {"error": "recipient_mode=explicit requires to_email or contact_id"}

        if to_email_arg:
            recipient_override = {
                "to_email": str(to_email_arg).strip(),
                "contact_name": None,
            }
        elif contact_id:
            row = conn.execute(
                "SELECT display_name, primary_email FROM contacts WHERE contact_id = ?",
                (contact_id,),
            ).fetchone()
            if row:
                primary_email = row["primary_email"] if hasattr(row, "keys") else row[1]
                display_name = row["display_name"] if hasattr(row, "keys") else row[0]
                if primary_email:
                    recipient_override = {
                        "to_email": primary_email,
                        "contact_name": display_name,
                    }

        envelope = resolve_draft_envelope(
            conn,
            project_id,
            for_reply=(recipient_mode != "project_primary"),
        )
        if recipient_override is None and envelope.to_email:
            recipient_override = {
                "to_email": envelope.to_email,
                "contact_name": envelope.contact_name,
            }

        preliminary_to_email = (recipient_override or {}).get("to_email") or envelope.to_email
        requested_thread_mode = str(
            arguments.get("thread_mode") or "auto"
        ).strip().casefold()
        if requested_thread_mode not in {"auto", "reply", "standalone"}:
            return {"error": f"Unsupported thread_mode: {requested_thread_mode}"}

        from src.agent.draft_recipient_guard import check_draft_recipient, is_internal_email

        trusted_internal_handoff = bool(
            preliminary_to_email
            and is_internal_email(preliminary_to_email)
            and internal_recipient_was_requested(operator_text, preliminary_to_email)
        )
        auto_standalone = bool(
            trusted_internal_handoff
            and (trusted_team_recipient or to_email_arg or contact_id)
        )
        resolved_thread_mode = (
            "standalone"
            if requested_thread_mode == "standalone"
            else "reply"
            if requested_thread_mode == "reply"
            else "standalone"
            if auto_standalone
            else "reply"
        )
        allow_internal_handoff = bool(
            resolved_thread_mode == "standalone" and trusted_internal_handoff
        )
        preliminary_check = check_draft_recipient(
            conn,
            project_id=project_id,
            to_email=preliminary_to_email,
            allow_trusted_internal_handoff=allow_internal_handoff,
        )
        if not preliminary_check.get("ok"):
            return {
                "error": preliminary_check.get("error")
                or "Draft recipient is not allowed",
                "suggested_external": preliminary_check.get("suggested_external"),
            }

        spec = build_directive_spec(
            operator_text,
            background_context=background_context,
            case_facts=load_case_facts(conn, project_id),
        )
        if spec.requires_clarification:
            return clarification_result(
                conn,
                spec=spec,
                attempts=attempts,
                thread_mode=resolved_thread_mode,
                to_email=preliminary_to_email,
                execution_context=context,
            )

        signal_type = (
            "internal_follow_up"
            if resolved_thread_mode == "standalone" and trusted_internal_handoff
            else (arguments.get("signal_type") or "inbound_reply")
        )
        generation_kwargs: dict[str, Any] = {}
        if operator_text:
            generation_kwargs["operator_directive"] = operator_text
        generated = generate_email_draft(
            conn,
            project_id,
            signal_type,
            context_hints,
            recipient_override=recipient_override,
            **generation_kwargs,
        )
        subject, body, generated_to_email = generated

        fidelity_issues = validate_draft_fidelity(body, spec)
        content_issues = (
            pm_uat_draft_issues(body)
            if PM_UAT_CANONICAL_PATH in wiki_paths
            else []
        )
        attempts.append({
            "attempt": 1,
            "fidelity_issues": fidelity_issues,
            "content_issues": content_issues,
        })
        if fidelity_issues or content_issues:
            feedback_parts: list[str] = []
            if fidelity_issues:
                feedback_parts.append(retry_instruction(spec, fidelity_issues))
            if content_issues:
                feedback_parts.append(pm_uat_correction(content_issues))
            retry_kwargs = dict(generation_kwargs)
            retry_kwargs["validation_feedback"] = "\n\n".join(feedback_parts)
            generated = generate_email_draft(
                conn,
                project_id,
                signal_type,
                context_hints,
                recipient_override=recipient_override,
                **retry_kwargs,
            )
            subject, body, generated_to_email = generated
            fidelity_issues = validate_draft_fidelity(body, spec)
            content_issues = (
                pm_uat_draft_issues(body)
                if PM_UAT_CANONICAL_PATH in wiki_paths
                else []
            )
            attempts.append({
                "attempt": 2,
                "fidelity_issues": fidelity_issues,
                "content_issues": content_issues,
            })
            if fidelity_issues:
                return fidelity_failure_result(
                    conn,
                    spec=spec,
                    attempts=attempts,
                    thread_mode=resolved_thread_mode,
                    to_email=preliminary_to_email,
                    execution_context=context,
                    issues=fidelity_issues,
                )
            if content_issues:
                return {
                    "error": "PM UAT draft failed completeness validation after retry: "
                    + ", ".join(content_issues)
                }

        final_to_email = (
            generated_to_email
            or (recipient_override or {}).get("to_email")
            or envelope.to_email
        )
        recipient_check = check_draft_recipient(
            conn,
            project_id=project_id,
            to_email=final_to_email,
            allow_trusted_internal_handoff=allow_internal_handoff,
        )
        if not recipient_check.get("ok"):
            return {
                "error": recipient_check.get("error")
                or "Draft recipient is not allowed",
                "suggested_external": recipient_check.get("suggested_external"),
            }

        if resolved_thread_mode == "reply":
            if envelope.original_subject:
                original = envelope.original_subject
                subject = (
                    original
                    if original.casefold().startswith("re:")
                    else f"Re: {original}"
                )
            cc = envelope.cc
            context_packet_ref = envelope.reply_to_email_id
            task_type = "reply_to_client"
        else:
            subject = _standalone_subject(subject, project_id)
            cc = _normalize_cc(arguments.get("cc"))
            context_packet_ref = None
            task_type = "internal_follow_up"

        from src.email.draft_session import DraftGenerationLink

        generation_link = None
        trace_id = getattr(generated, "trace_id", None)
        output_revision_id = getattr(generated, "output_revision_id", None)
        if trace_id and output_revision_id:
            generation_link = DraftGenerationLink(trace_id, output_revision_id)

        # A read-only caller gives this tool its own writer; do not let the
        # draft helper commit or publish while that writer is still leased.
        owns_writer = _connection_is_query_only(conn)
        post_commit = []
        with draft_write_connection(conn, context) as wconn:
            create_kwargs = {
                "project_id": project_id,
                "task_type": task_type,
                "subject": subject,
                "body": body,
                "to_email": final_to_email,
                "cc": cc,
                "context_packet_ref": context_packet_ref,
                "generation_link": generation_link,
            }
            if owns_writer:
                create_kwargs.update(commit=False, post_commit=post_commit)
            draft_id = create_draft_fn(wconn, **create_kwargs)
            record_draft_fidelity_event(
                wconn,
                status="ok",
                spec=spec,
                attempts=attempts,
                thread_mode=resolved_thread_mode,
                to_email=final_to_email,
                execution_context=context,
                draft_id=draft_id,
            )
        for effect in post_commit:
            effect()

        doc_keys = resolve_draft_doc_keys(
            arguments.get("doc_keys"),
            background_context,
        )
        attachment_kwargs = (
            {"write_scope": context.get("write_connection_factory")}
            if owns_writer else {"conn": conn}
        )
        try:
            attached, failed = attach_docs_to_draft(
                draft_id, doc_keys,
                actor_user_id=str(context.get("operator_id") or "system:draft-reply"),
                **attachment_kwargs,
            )
        except (sqlite3.Error, OSError, AttachmentRecoveryError):
            logger.exception("Attachment outcome unknown for committed draft %s", draft_id)
            # The draft already committed. A journal receipt may also have
            # committed even if its writer raised while exiting. Do not turn
            # that into an unqualified failure or automatically retry it.
            return {
                "status": "created",
                "draft_id": draft_id,
                "subject": subject,
                "body": body,
                "to_email": final_to_email,
                "thread_mode": resolved_thread_mode,
                "attachments": [],
                "attachments_failed": doc_keys,
                "attachment_outcome": "unknown",
                "message": "Draft saved, but attachment outcome is unknown. Verify in Dashboard before retrying.",
            }
        return {
            "status": "created",
            "draft_id": draft_id,
            "subject": subject,
            "body": body,
            "to_email": final_to_email,
            "thread_mode": resolved_thread_mode,
            "attachments": attached,
            "attachments_failed": failed,
            "message": (
                "Draft saved, but requested documents were not attached: "
                + ", ".join(failed)
                + ". Review attachment coverage before sending via Dashboard Drafts or /pending."
                if failed else "Draft saved. Review and send via Dashboard Drafts or /pending."
            ),
        }
    except LLMParseFailure as exc:
        return {"error": f"LLM failed to generate draft: {exc}"}
    except ValueError as exc:
        return {"error": str(exc)}
    except Exception as exc:
        return {"error": str(exc)}
