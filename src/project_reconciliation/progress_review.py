"""Review-only escalation and approval revalidation for historical progress."""
from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable

from src.events.bus import Event, event_bus

from src.project_reconciliation.delivery_confirmation import load_delivery_confirmations
from src.project_reconciliation.contracts import FrozenEvidenceBundle, ReconciliationMode
from src.project_reconciliation.evidence import freeze_email_bundle
from src.project_reconciliation.progress_facts import (
    CANONICAL_PROGRESS_PREDICATES, resolve_bundle_authorities,
)


logger = logging.getLogger(__name__)


def has_contract_progress(assessment: dict | None) -> bool:
    return any(
        fact.get("predicate") in CANONICAL_PROGRESS_PREDICATES
        for fact in (assessment or {}).get("extracted_facts", [])
    )


def preview_lagging_project(
    conn, *, project_id: str, source_ref: str,
    post_commit: list[Callable[[], None]] | None = None,
):
    """Create a deduplicated preview without committing a caller's transaction."""
    from src.project_reconciliation.service import reconcile_project_from_emails
    row = conn.execute(
        "SELECT operator_id FROM projects WHERE project_id=?", (project_id,),
    ).fetchone()
    if row is None or not row[0]:
        return None
    digest = hashlib.sha256(f"{project_id}:{source_ref}".encode()).hexdigest()
    caller_transaction = conn.in_transaction
    result = reconcile_project_from_emails(
        conn, project_id=project_id, mode=ReconciliationMode.PREVIEW,
        operator_id=row[0], chat_id=f"email:{project_id}", trusted_surface="email",
        trusted_explicit_operator=False, request_key=f"milestone-lag-v1:{digest}",
        create_if_missing=False, trigger_kind="email",
    )
    if result.status == "needs_review" and result.proposed_update_id:
        review_id = result.proposed_update_id

        def publish_review() -> None:
            try:
                event_bus.publish(Event(
                    event_type="project_updated", project_id=project_id, surface="email",
                    detail={"review_id": review_id, "source": "milestone_lag_review", "fields": []},
                ))
            except Exception:
                # A committed review is not erased by an SSE failure.
                logger.exception("Could not publish lag review for %s", project_id)

        if caller_transaction:
            if post_commit is not None:
                post_commit.append(publish_review)
        else:
            publish_review()
    return result


def validate_progress_review(conn, data: dict, changes: list[dict]) -> str | None:
    """Re-evaluate canonical historical claims against current role bindings."""
    if data.get("task_type") != "email_reconciliation":
        return None
    try:
        assessment = json.loads(data.get("assessment_json") or "{}")
        if not has_contract_progress(assessment):
            return None
        row = conn.execute(
            "SELECT checkpoint_json FROM pending_operations WHERE operation_id=?",
            (data.get("operation_id"),),
        ).fetchone()
        bundle = FrozenEvidenceBundle.model_validate(json.loads(row[0])["bundle"])
        project = conn.execute(
            "SELECT * FROM projects WHERE project_id=?", (data["project_id"],),
        ).fetchone()
        if project is None or bundle.project_id != data["project_id"]:
            return "Historical evidence identity is invalid; create a fresh review."
        current_bundle = freeze_email_bundle(conn, data["project_id"])
        if current_bundle.bundle_hash != bundle.bundle_hash:
            return "Historical email evidence changed after this proposal; create a fresh review."
        from src.project_reconciliation.replay import build_proposal
        proposal = build_proposal(
            bundle, current_project=dict(project),
            source_authorities=resolve_bundle_authorities(conn, bundle),
            delivery_confirmations=load_delivery_confirmations(conn, bundle),
        )
        def confirmation_refs(value):
            return {fact.get("subject") for fact in (value or {}).get("extracted_facts", [])
                if str(fact.get("subject") or "").startswith("delivery-confirmation:")}
        if confirmation_refs(assessment) != confirmation_refs(proposal.assessment):
            return "Delivery source confirmation changed or was revoked; create a fresh review."
        for change in changes:
            if change["field"] == "current_milestone" and proposal.fields.get("current_milestone") != change.get("new_value"):
                return "Historical progress no longer satisfies the current evidence/authority contract; create a fresh review."
    except (ValueError, TypeError, KeyError, IndexError):
        return "Historical progress checkpoint is invalid; create a fresh review."
    return None
