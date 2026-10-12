"""Durable orchestration for reconciling project truth from historical email."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable

from src.db.borrowed_transaction import borrowed_command_savepoint

from src.governance.four_eyes import is_high_risk_project_fields, team_four_eyes_enabled
from src.models.enums import Milestone, ProjectStatus
from src.models.project import Project
from src.project_reconciliation.delivery_confirmation import load_delivery_confirmations
from src.project_reconciliation.contracts import (
    ReconciliationMode,
    ReconciliationResult,
)
from src.project_reconciliation.evidence import (
    freeze_email_bundle,
    validate_exact_citations,
)
from src.project_reconciliation.replay import build_proposal, reviewable_recon_fields
from src.project_reconciliation.progress_facts import resolve_bundle_authorities
from src.project_reconciliation.progress_review import has_contract_progress
from src.project_reconciliation.repository import (
    close_notes_only_reconciliation_reviews,
    close_review_proposal_as_superseded,
    create_operation,
    get_operation,
    get_operation_by_request_key,
    link_latest_accepted_write,
    refresh_review_proposal,
    store_review_proposal,
    update_operation,
)
from src.project_reconciliation.service_support import (
    checkpoint_models,
    make_result,
    operation_is_owned,
    persist_result,
    persist_result_and_supersede_siblings,
    publish_project_updated,
    project_row,
    stored_result,
)
from src.repositories.project_repo import create_project
from src.write_layer.write_helpers import safe_write_project


logger = logging.getLogger(__name__)

_TERMINAL_PHASES = {"committed", "no_change", "superseded"}


def reconcile_project_from_emails(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    mode: ReconciliationMode | str,
    operator_id: str,
    chat_id: str,
    trusted_surface: str,
    trusted_explicit_operator: bool,
    request_key: str | None = None,
    resume_operation_id: str | None = None,
    create_if_missing: bool = False,
    trigger_kind: str = "operator",
    post_commit: list[Callable[[], None]] | None = None,
) -> ReconciliationResult:
    """Run an owned command or stage a borrowed command in the caller's scope.

    A borrowed COMMIT requires the caller's post-commit effect list. Its returned
    receipt is pending until that caller successfully closes its transaction.
    """
    options = dict(
        project_id=project_id, mode=mode, operator_id=operator_id,
        chat_id=chat_id, trusted_surface=trusted_surface,
        trusted_explicit_operator=trusted_explicit_operator,
        request_key=request_key, resume_operation_id=resume_operation_id,
        create_if_missing=create_if_missing, trigger_kind=trigger_kind,
    )
    if conn.in_transaction and mode in (ReconciliationMode.PREVIEW, "preview"):
        with borrowed_command_savepoint(conn) as preview_conn:
            return _reconcile_project_from_emails(preview_conn, **options)
    if conn.in_transaction and mode in (ReconciliationMode.COMMIT, "commit"):
        # Preserve the existing authorization outcome without touching the scope.
        if not trusted_explicit_operator:
            return _reconcile_project_from_emails(conn, **options)
        if post_commit is None:
            return make_result(
                status="denied", outcome="rejected", project_id=project_id,
                operation_id=resume_operation_id or "not_started",
                error_code="commit_requires_post_commit_scope",
                error="A caller-owned transaction must supply post-commit effects.",
            )
        effects: list[Callable[[], None]] = []
        with borrowed_command_savepoint(conn) as command_conn:
            result = _reconcile_project_from_emails(
                command_conn, **options, post_commit=effects,
            )
        post_commit.extend(effects)
        if result.committed:
            return result.model_copy(update={
                "status": "pending_commit", "committed": False,
                "text": f"{project_id}: verify receipt durability after the caller transaction closes.",
            })
        return result
    return _reconcile_project_from_emails(conn, **options)


def _reconcile_project_from_emails(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    mode: ReconciliationMode | str,
    operator_id: str,
    chat_id: str,
    trusted_surface: str,
    trusted_explicit_operator: bool,
    request_key: str | None = None,
    resume_operation_id: str | None = None,
    create_if_missing: bool = False,
    trigger_kind: str = "operator",
    post_commit: list[Callable[[], None]] | None = None,
) -> ReconciliationResult:
    """Preview or commit one frozen, policy-evaluated reconciliation operation."""
    project_id = project_id.strip().upper()
    operation_id = resume_operation_id or "not_started"
    try:
        selected_mode = mode if isinstance(mode, ReconciliationMode) else ReconciliationMode(mode)
    except ValueError:
        return make_result(
            status="failed",
            outcome="rejected",
            project_id=project_id,
            operation_id=operation_id,
            error_code="invalid_mode",
            error=f"Unsupported reconciliation mode: {mode}",
        )

    if selected_mode == ReconciliationMode.COMMIT and not trusted_explicit_operator:
        return make_result(
            status="denied",
            outcome="rejected",
            project_id=project_id,
            operation_id=operation_id,
            error_code="operator_authorization_required",
            error="Commit requires a trusted explicit operator instruction.",
        )

    operation = None
    if resume_operation_id:
        operation = get_operation(conn, resume_operation_id)
    elif request_key:
        operation = get_operation_by_request_key(conn, request_key)

    if operation is not None:
        operation_id = str(operation["operation_id"])
        if not operation_is_owned(
            operation,
            project_id=project_id,
            operator_id=operator_id,
            chat_id=chat_id,
            trusted_surface=trusted_surface,
        ):
            return make_result(
                status="denied",
                outcome="rejected",
                project_id=project_id,
                operation_id=operation_id,
                error_code="operation_scope_mismatch",
                error="The reconciliation operation belongs to another scope.",
            )
        if operation.get("phase") in _TERMINAL_PHASES:
            stored = stored_result(operation)
            if stored is not None:
                return stored
        if selected_mode == ReconciliationMode.PREVIEW and operation.get("phase") == "needs_review":
            stored = stored_result(operation)
            if stored is not None:
                return stored
    else:
        operation = create_operation(
            conn,
            operator_id=operator_id,
            chat_id=chat_id,
            project_id=project_id,
            request_key=request_key,
            requested_commit=selected_mode == ReconciliationMode.COMMIT,
            create_if_missing=create_if_missing,
            trusted_surface=trusted_surface,
            trigger_kind=trigger_kind,
            payload={"mode": selected_mode.value},
        )
        operation_id = str(operation["operation_id"])
        conn.commit()

    durable_result: ReconciliationResult | None = None
    try:
        close_notes_only_reconciliation_reviews(conn, project_id=project_id)
        update_operation(
            conn,
            operation_id,
            phase="evaluating",
            increment_attempt=True,
        )
        conn.commit()
        operation = get_operation(conn, operation_id) or operation
        bundle, proposal = checkpoint_models(operation)
        if bundle is None or proposal is None:
            current = project_row(conn, project_id)
            bundle = freeze_email_bundle(conn, project_id)
            proposal = build_proposal(bundle, current_project=current,
                source_authorities=resolve_bundle_authorities(conn, bundle),
                delivery_confirmations=load_delivery_confirmations(conn, bundle))
            validate_exact_citations(bundle, proposal.citations)
            checkpoint = {
                "bundle": bundle.model_dump(mode="json"),
                "proposal": proposal.model_dump(mode="json"),
            }
            update_operation(
                conn,
                operation_id,
                phase="evaluating",
                checkpoint=checkpoint,
                base_version=proposal.base_version,
                project_id=project_id if current else None,
            )
            conn.commit()

        source_ref = f"email-bundle:{bundle.bundle_hash}"
        allow_create = create_if_missing or bool(operation.get("create_if_missing"))

        if selected_mode == ReconciliationMode.PREVIEW:
            review_fields = reviewable_recon_fields(proposal.fields)
            proposed_update_id = store_review_proposal(
                conn,
                operation_id=operation_id,
                project_id=project_id,
                fields=review_fields,
                confidence=proposal.confidence,
                source_ref=source_ref,
                operator_id=operator_id,
                assessment=proposal.assessment,
            )
            if review_fields:
                result = make_result(
                    status="needs_review",
                    outcome="proposed",
                    project_id=project_id,
                    operation_id=operation_id,
                    proposed_fields=review_fields,
                    current_project=project_row(conn, project_id),
                    bundle_hash=bundle.bundle_hash,
                    evidence_refs=proposal.evidence_refs,
                    proposed_update_id=proposed_update_id,
                    text=f"{project_id} email reconciliation is ready for review.",
                )
                persist_result_and_supersede_siblings(
                    conn,
                    operation_id,
                    result,
                    phase="needs_review",
                    operator_id=operator_id,
                    chat_id=chat_id,
                    project_id=project_id,
                    proposed_update_id=proposed_update_id,
                )
                return result
            result = make_result(
                status="no_change",
                outcome="no_change",
                project_id=project_id,
                operation_id=operation_id,
                current_project=project_row(conn, project_id),
                bundle_hash=bundle.bundle_hash,
                evidence_refs=proposal.evidence_refs,
                text=f"{project_id} has no evidence-backed changes to apply.",
            )
            close_review_proposal_as_superseded(
                conn,
                update_id=operation.get("proposed_update_id"),
                reason="The latest preview found no evidence-backed changes.",
            )
            persist_result_and_supersede_siblings(
                conn,
                operation_id,
                result,
                phase="no_change",
                operator_id=operator_id,
                chat_id=chat_id,
                project_id=project_id,
            )
            return result

        current = project_row(conn, project_id)
        snapshot_changed = (
            proposal.project_exists != (current is not None)
            or (
                proposal.project_exists
                and current is not None
                and current.get("updated_at") != proposal.base_version
            )
        )
        if snapshot_changed:
            # Rebase the frozen evidence instead of forcing a phantom approval.
            proposal = build_proposal(bundle, current_project=current,
                source_authorities=resolve_bundle_authorities(conn, bundle),
                delivery_confirmations=load_delivery_confirmations(conn, bundle))
            validate_exact_citations(bundle, proposal.citations)
            checkpoint = {
                "bundle": bundle.model_dump(mode="json"),
                "proposal": proposal.model_dump(mode="json"),
            }
            update_operation(
                conn,
                operation_id,
                phase="evaluating",
                checkpoint=checkpoint,
                base_version=proposal.base_version,
                project_id=project_id if current else None,
            )
            operation = get_operation(conn, operation_id) or operation
            existing_review_id = operation.get("proposed_update_id")
            if proposal.fields:
                refresh_review_proposal(
                    conn,
                    update_id=existing_review_id,
                    project_id=project_id,
                    fields=proposal.fields,
                    confidence=proposal.confidence,
                    source_ref=source_ref,
                    assessment=proposal.assessment,
                )
            else:
                close_review_proposal_as_superseded(
                    conn,
                    update_id=existing_review_id,
                    reason="Project truth already matches the refreshed reconciliation.",
                )
            conn.commit()

        if current is None and not allow_create:
            result = make_result(
                status="needs_review",
                outcome="missing_project",
                project_id=project_id,
                operation_id=operation_id,
                proposed_fields=proposal.fields,
                bundle_hash=bundle.bundle_hash,
                evidence_refs=proposal.evidence_refs,
                error_code="project_missing",
                error="Project creation requires create_if_missing=true.",
            )
            persist_result(conn, operation_id, result, phase="needs_review")
            return result

        if not bundle.records:
            result = make_result(
                status="no_change",
                outcome="no_evidence",
                project_id=project_id,
                operation_id=operation_id,
                current_project=current,
                bundle_hash=bundle.bundle_hash,
                text=f"No historical email evidence was found for {project_id}.",
            )
            close_review_proposal_as_superseded(
                conn,
                update_id=operation.get("proposed_update_id"),
                reason="No historical email evidence remains to review.",
            )
            persist_result_and_supersede_siblings(
                conn,
                operation_id,
                result,
                phase="no_change",
                operator_id=operator_id,
                chat_id=chat_id,
                project_id=project_id,
            )
            return result

        if current is not None and not proposal.fields:
            result = make_result(
                status="no_change",
                outcome="no_change",
                project_id=project_id,
                operation_id=operation_id,
                current_project=current,
                bundle_hash=bundle.bundle_hash,
                evidence_refs=proposal.evidence_refs,
                text=f"{project_id}: no evidence-backed project-state change was established.",
            )
            close_review_proposal_as_superseded(
                conn,
                update_id=operation.get("proposed_update_id"),
                reason="Project truth already matches the refreshed reconciliation.",
            )
            persist_result_and_supersede_siblings(
                conn,
                operation_id,
                result,
                phase="no_change",
                operator_id=operator_id,
                chat_id=chat_id,
                project_id=project_id,
            )
            return result

        project_created = False
        if current is None:
            create_project(
                conn,
                Project(
                    project_id=project_id,
                    client_name=project_id,
                    project_status=ProjectStatus.ACTIVE,
                    current_milestone=Milestone.M1_NETWORK_SETUP,
                    operator_id=operator_id,
                ),
                commit=False,
            )
            project_created = True

        high_risk, high_reason = is_high_risk_project_fields(
            conn, project_id, dict(proposal.fields),
        )
        inferred_progress = (
            "current_milestone" in proposal.fields and has_contract_progress(proposal.assessment)
        )
        if inferred_progress or (high_risk and team_four_eyes_enabled()):
            proposed_update_id = operation.get("proposed_update_id")
            if not proposed_update_id:
                proposed_update_id = store_review_proposal(
                    conn,
                    operation_id=operation_id,
                    project_id=project_id,
                    fields=proposal.fields,
                    confidence=proposal.confidence,
                    source_ref=source_ref,
                    operator_id=operator_id,
                    assessment=proposal.assessment,
                )
            result = make_result(
                status="needs_review",
                outcome="needs_review",
                project_id=project_id,
                operation_id=operation_id,
                project_created=project_created,
                proposed_fields=proposal.fields,
                current_project=project_row(conn, project_id),
                bundle_hash=bundle.bundle_hash,
                evidence_refs=proposal.evidence_refs,
                proposed_update_id=proposed_update_id,
                error_code="milestone_review_required" if inferred_progress else "four_eyes_required",
                error="Inferred milestone progress requires operator review." if inferred_progress else high_reason or "High-risk reconciliation requires a second person.",
                text=f"{project_id} email reconciliation is waiting for operator review.",
            )
            persist_result_and_supersede_siblings(
                conn,
                operation_id,
                result,
                phase="needs_review",
                operator_id=operator_id,
                chat_id=chat_id,
                project_id=project_id,
                proposed_update_id=proposed_update_id,
            )
            return result

        write_result = safe_write_project(
            conn,
            project_id,
            dict(proposal.fields),
            actor=operator_id,
            evidence_ref=source_ref,
            operator_confirmed=True,
            reason=f"Explicit email reconciliation operation {operation_id}",
            surface=trusted_surface,
            commit=False,
            publish_event=False,
            expected_updated_at=proposal.base_version if not project_created else None,
        )
        if write_result.outcome not in {"accepted", "no_change"}:
            conn.rollback()
            proposed_update_id = operation.get("proposed_update_id")
            if write_result.outcome == "needs_review" and not proposed_update_id:
                proposed_update_id = store_review_proposal(
                    conn,
                    operation_id=operation_id,
                    project_id=project_id,
                    fields=proposal.fields,
                    confidence=proposal.confidence,
                    source_ref=source_ref,
                    operator_id=operator_id,
                    assessment=proposal.assessment,
                )
            latest = project_row(conn, project_id)
            stale_version = (
                write_result.outcome == "rejected" and not project_created
                and latest is not None and latest.get("updated_at") != proposal.base_version
            )
            result = make_result(
                status="failed" if stale_version else "needs_review",
                outcome=write_result.outcome,
                project_id=project_id,
                operation_id=operation_id,
                proposed_fields=proposal.fields,
                current_project=latest,
                bundle_hash=bundle.bundle_hash,
                evidence_refs=proposal.evidence_refs,
                proposed_update_id=proposed_update_id,
                error_code="stale_project_version" if stale_version else "safe_write_rejected",
                error=write_result.rejection_reason or write_result.needs_review_reason,
            )
            persist_result(
                conn,
                operation_id,
                result,
                phase=result.status,
                proposed_update_id=proposed_update_id,
            )
            return result

        proposed_update_id = operation.get("proposed_update_id")
        if proposed_update_id:
            conn.execute(
                """UPDATE proposed_updates
                      SET decision_outcome='accepted', decided_at=datetime('now')
                    WHERE update_id=? AND decision_outcome='needs_review'""",
                (proposed_update_id,),
            )
        accepted_id = link_latest_accepted_write(
            conn,
            project_id=project_id,
            operation_id=operation_id,
            source_ref=source_ref,
            assessment=proposal.assessment,
        ) or proposed_update_id
        current_after = project_row(conn, project_id)
        committed = project_created or write_result.outcome == "accepted"
        phase = "committed" if committed else "no_change"
        outcome = "accepted" if committed else "no_change"
        result = make_result(
            status=phase,
            outcome=outcome,
            committed=committed,
            project_id=project_id,
            operation_id=operation_id,
            project_created=project_created,
            applied_fields=write_result.applied_fields,
            proposed_fields=proposal.fields,
            current_project=current_after,
            bundle_hash=bundle.bundle_hash,
            evidence_refs=proposal.evidence_refs,
            proposed_update_id=accepted_id,
            text=(
                f"{project_id} project state was updated from frozen historical email evidence."
                if committed and reviewable_recon_fields(write_result.applied_fields)
                else f"{project_id}: only reconciliation notes were saved; project state is unchanged."
                if committed else f"{project_id}: no evidence-backed project-state change was established."
            ),
        )
        persist_result_and_supersede_siblings(
            conn,
            operation_id,
            result,
            phase=phase,
            operator_id=operator_id,
            chat_id=chat_id,
            project_id=project_id,
            proposed_update_id=accepted_id,
            commit_request_key=(
                request_key if committed and trusted_surface == "ai_workspace" else None
            ),
        )
        if committed and post_commit is None and not conn.in_transaction:
            durable_result = result
        if committed:
            def publish_committed_update() -> None:
                try:
                    publish_project_updated(
                        project_id=project_id, operation_id=operation_id,
                        fields=list(write_result.applied_fields), surface=trusted_surface,
                    )
                except Exception:
                    # A notification failure cannot undo a committed receipt.
                    logger.exception(
                        "Could not publish committed reconciliation update for %s (operation %s)",
                        project_id, operation_id,
                    )

            if post_commit is not None:
                post_commit.append(publish_committed_update)
            else:
                publish_committed_update()
        return result
    except Exception as exc:
        if durable_result is not None:
            # Post-commit effects cannot turn a durable accepted receipt into failure.
            return durable_result
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        try:
            operation = get_operation(conn, operation_id)
            if operation is None:
                operation = create_operation(
                    conn,
                    operator_id=operator_id,
                    chat_id=chat_id,
                    project_id=project_id,
                    request_key=request_key,
                    requested_commit=selected_mode == ReconciliationMode.COMMIT,
                    create_if_missing=create_if_missing,
                    trusted_surface=trusted_surface,
                    trigger_kind=trigger_kind,
                )
                operation_id = str(operation["operation_id"])
            result = make_result(
                status="failed",
                outcome="failed",
                project_id=project_id,
                operation_id=operation_id,
                current_project=project_row(conn, project_id),
                error_code="reconciliation_failed",
                error=str(exc),
                text=f"{project_id} reconciliation failed without changing project truth.",
            )
            persist_result(
                conn,
                operation_id,
                result,
                phase="failed",
                last_error=str(exc),
            )
            return result
        except Exception:
            return make_result(
                status="failed",
                outcome="failed",
                project_id=project_id,
                operation_id=operation_id,
                error_code="reconciliation_failed",
                error=str(exc),
            )
