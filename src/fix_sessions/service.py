"""Application service for trustworthy FIX session configuration updates."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any

from src.events.bus import Event, event_bus
from src.repositories.activity_log_repo import insert_activity

from .models import ConfirmationMode, EmailAuthentication, FixEnvironment, FixSessionCandidate, FixSessionSnapshot
from .repository import create_candidate, confirm_candidate, get_candidate, get_current_snapshot, get_sender_policy
from .secret_store import SessionSecretStore, make_secret_reference

_FIELDS = ("begin_string", "sender_comp_id", "target_comp_id", "heartbeat_seconds", "reset_seq_num_on_logon", "account_reference", "endpoints", "metadata")


@dataclass(frozen=True)
class CandidateDecision:
    candidate_id: str
    status: str
    session_version_id: str | None
    reasons: tuple[str, ...]


def _canonical(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_canonical(v) for v in value]
    return value


def _build_snapshot(project_id: str, environment: str, session_key: str, update: dict[str, Any], current: FixSessionSnapshot | None) -> tuple[FixSessionSnapshot, list[str], list[str], dict[str, dict[str, Any]]]:
    base = current.model_dump(mode="json") if current else {}
    payload: dict[str, Any] = {"project_id": project_id, "environment": environment, "session_key": session_key}
    changed: list[str] = []
    inherited: list[str] = []
    sources: dict[str, dict[str, Any]] = {}
    for field in _FIELDS:
        if field in update and update[field] not in (None, [], {}):
            payload[field] = update[field]
            if not current or _canonical(update[field]) != _canonical(base.get(field)):
                changed.append(field)
            sources[field] = {"extraction_method": "deterministic"}
        elif field in base:
            payload[field] = base[field]
            inherited.append(field)
            sources[field] = {"extraction_method": "inherited"}
    snapshot = FixSessionSnapshot(**payload)
    if snapshot.sender_comp_id == snapshot.target_comp_id:
        raise ValueError("SenderCompID and TargetCompID must be different")
    route_roles = [endpoint.route_role.value for endpoint in snapshot.endpoints]
    if len(route_roles) != len(set(route_roles)):
        raise ValueError("duplicate endpoint route roles are not allowed")
    return snapshot, changed, inherited, sources


def _policy_reasons(policy: sqlite3.Row | None, authentication: EmailAuthentication, conflicts: list[str]) -> list[str]:
    reasons = list(conflicts)
    if policy is None:
        reasons.append("sender_not_allowlisted")
        return reasons
    if not bool(policy["auto_confirm_enabled"]):
        reasons.append("sender_auto_confirm_disabled")
    checks = (("require_spf", authentication.spf.value, "spf_not_pass"), ("require_dkim", authentication.dkim.value, "dkim_not_pass"), ("require_dmarc", authentication.dmarc.value, "dmarc_not_pass"))
    for column, result, reason in checks:
        if bool(policy[column]) and result != "pass":
            reasons.append(reason)
    if bool(policy["require_alignment"]) and not authentication.aligned_sender:
        reasons.append("sender_alignment_failed")
    return reasons


def ingest_configuration_update(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    environment: str,
    update: dict[str, Any],
    sender_email: str | None,
    authentication: EmailAuthentication,
    source_email_id: str | None = None,
    source_attachment_id: str | None = None,
    evidence_id: str | None = None,
    received_at: str | None = None,
    session_key: str = "order_entry",
    idempotency_key: str | None = None,
    secret_store: SessionSecretStore | None = None,
    auto_confirm: bool = True,
) -> CandidateDecision:
    """Create a candidate first, then auto-confirm only under the exact trust policy."""
    env = FixEnvironment("PRODUCTION" if environment.upper() in {"PROD", "PRODUCTION"} else "UAT")
    current = get_current_snapshot(conn, project_id, env, session_key)
    candidate_id = "fsc_" + uuid.uuid4().hex
    mutable_update = dict(update)
    secrets = dict(mutable_update.pop("secrets", {}) or {})
    snapshot, changed, inherited, field_sources = _build_snapshot(project_id, env.value, session_key, mutable_update, current)
    if not changed and current:
        conflicts = ["no_configuration_change"]
    else:
        conflicts = []
    policy = get_sender_policy(conn, sender_email)
    policy_reasons = _policy_reasons(policy, authentication, conflicts)
    completeness = 1.0 if snapshot.sender_comp_id and snapshot.target_comp_id and snapshot.endpoints else 0.0
    candidate = FixSessionCandidate(
        candidate_id=candidate_id, snapshot=snapshot, source_email_id=source_email_id,
        source_attachment_id=source_attachment_id, evidence_id=evidence_id, sender_email=sender_email,
        sender_policy_id=policy["policy_id"] if policy else None, authentication=authentication,
        confidence=1.0, completeness=completeness, changed_fields=changed, inherited_fields=inherited,
        conflict_reasons=policy_reasons, field_sources=field_sources, received_at=received_at,
    )
    stored_id = create_candidate(conn, candidate, idempotency_key=idempotency_key)
    if stored_id != candidate_id:
        existing = get_candidate(conn, stored_id)
        return CandidateDecision(stored_id, existing.status.value if existing else "pending_review", None, tuple(existing.conflict_reasons if existing else ()))

    version_id: str | None = None
    status = "pending_review"
    if auto_confirm and not policy_reasons:
        next_version = (conn.execute("SELECT COALESCE(MAX(version_no),0)+1 FROM project_fix_sessions WHERE project_id=? AND environment=? AND session_key=?", (project_id, env.value, session_key)).fetchone()[0])
        if secrets:
            if secret_store is None:
                policy_reasons.append("secret_store_unavailable")
            else:
                data = snapshot.model_dump(mode="json")
                for name, value in secrets.items():
                    reference = make_secret_reference(project_id, env.value, session_key, name, next_version)
                    secret_store.put(reference, value)
                    data[f"{name}_secret_reference"] = reference
                candidate.snapshot = FixSessionSnapshot(**data)
                conn.execute("UPDATE fix_session_config_candidates SET proposed_snapshot_json=? WHERE candidate_id=?", (json.dumps(candidate.snapshot.model_dump(mode="json"), sort_keys=True), candidate_id))
        if not policy_reasons:
            version_id = confirm_candidate(conn, candidate, mode=ConfirmationMode.AUTO, confirmed_by=sender_email or "authenticated_email", reason="allowlisted sender and SPF/DKIM/DMARC/alignment passed")
            status = "confirmed"

    insert_activity(conn, actor=sender_email or "system", action_type="fix_session_candidate_" + status, project_id=project_id, surface="email", object_ref=candidate_id, summary=f"{env.value} FIX session candidate {status}", metadata_json=json.dumps({"changed_fields": changed, "inherited_fields": inherited, "reasons": policy_reasons, "session_version_id": version_id}))
    event_bus.publish(Event(event_type="fix_session_candidate_" + status, project_id=project_id, detail={"candidate_id": candidate_id, "environment": env.value, "session_version_id": version_id}))
    return CandidateDecision(candidate_id, status, version_id, tuple(policy_reasons))


def approve_candidate(conn: sqlite3.Connection, candidate_id: str, *, operator_id: str, reason: str) -> str:
    candidate = get_candidate(conn, candidate_id)
    if candidate is None:
        raise KeyError(candidate_id)
    if candidate.status.value == "confirmed":
        row = conn.execute("SELECT session_version_id FROM project_fix_sessions WHERE source_candidate_id=?", (candidate_id,)).fetchone()
        return row[0] if row else ""
    if candidate.status.value != "pending_review":
        raise ValueError(f"candidate is {candidate.status.value}")
    if candidate.completeness < 1.0:
        raise ValueError("candidate is incomplete and cannot be confirmed")
    version_id = confirm_candidate(conn, candidate, mode=ConfirmationMode.OPERATOR, confirmed_by=operator_id, reason=reason)
    insert_activity(conn, actor=operator_id, action_type="fix_session_candidate_confirmed", project_id=candidate.snapshot.project_id, surface="dashboard", object_ref=candidate_id, summary=f"{candidate.snapshot.environment.value} FIX session confirmed")
    event_bus.publish(Event(event_type="fix_session_candidate_confirmed", project_id=candidate.snapshot.project_id, detail={"candidate_id": candidate_id, "session_version_id": version_id}, surface="dashboard"))
    return version_id


def reject_candidate(conn: sqlite3.Connection, candidate_id: str, *, operator_id: str, reason: str) -> None:
    row = conn.execute("SELECT project_id, status FROM fix_session_config_candidates WHERE candidate_id=?", (candidate_id,)).fetchone()
    if row is None:
        raise KeyError(candidate_id)
    if row["status"] != "pending_review":
        raise ValueError(f"candidate is {row['status']}")
    conn.execute("UPDATE fix_session_config_candidates SET status='rejected', decision_mode='operator', decision_reason=?, decided_by=?, decided_at=datetime('now') WHERE candidate_id=?", (reason, operator_id, candidate_id))
    insert_activity(conn, actor=operator_id, action_type="fix_session_candidate_rejected", project_id=row["project_id"], surface="dashboard", object_ref=candidate_id, summary=reason)
