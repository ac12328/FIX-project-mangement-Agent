"""Persistence helpers for FIX session candidates and authoritative snapshots."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from .models import (
    CandidateStatus,
    ConfirmationMode,
    EmailAuthentication,
    FixEnvironment,
    FixSessionCandidate,
    FixSessionEndpoint,
    FixSessionSnapshot,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return default


def get_sender_policy(conn: sqlite3.Connection, email_address: str | None) -> sqlite3.Row | None:
    if not email_address:
        return None
    return conn.execute(
        """SELECT * FROM configuration_sender_policies
           WHERE lower(email_address) = lower(?)
             AND purpose = 'fix_session_configuration'
             AND active = 1""",
        (email_address.strip(),),
    ).fetchone()


def upsert_sender_policy(
    conn: sqlite3.Connection,
    *,
    email_address: str,
    approved_by: str,
    organisation_id: str | None = None,
    auto_confirm_enabled: bool = True,
    notes: str | None = None,
) -> str:
    policy_id = "fsp_" + uuid.uuid4().hex[:16]
    existing = conn.execute(
        "SELECT policy_id FROM configuration_sender_policies WHERE lower(email_address)=lower(?)",
        (email_address.strip(),),
    ).fetchone()
    if existing:
        policy_id = existing["policy_id"]
    conn.execute(
        """INSERT INTO configuration_sender_policies
           (policy_id, organisation_id, email_address, auto_confirm_enabled,
            active, approved_by, approved_at, notes)
           VALUES (?, ?, ?, ?, 1, ?, ?, ?)
           ON CONFLICT(email_address, purpose) DO UPDATE SET
             organisation_id=excluded.organisation_id,
             auto_confirm_enabled=excluded.auto_confirm_enabled,
             active=1, approved_by=excluded.approved_by,
             approved_at=excluded.approved_at, revoked_at=NULL, notes=excluded.notes""",
        (
            policy_id,
            organisation_id,
            email_address.strip().lower(),
            int(auto_confirm_enabled),
            approved_by,
            _utc_now(),
            notes,
        ),
    )
    return policy_id


def get_current_snapshot(
    conn: sqlite3.Connection,
    project_id: str,
    environment: str | FixEnvironment,
    session_key: str = "order_entry",
) -> FixSessionSnapshot | None:
    environment_value = environment.value if isinstance(environment, FixEnvironment) else str(environment).upper()
    row = conn.execute(
        """SELECT * FROM project_fix_sessions
           WHERE project_id=? AND environment=? AND session_key=? AND is_current=1""",
        (project_id, environment_value, session_key),
    ).fetchone()
    if not row:
        return None
    endpoints = conn.execute(
        """SELECT route_role, host, port, priority, transport
           FROM fix_session_endpoints WHERE session_version_id=?
           ORDER BY priority, endpoint_id""",
        (row["session_version_id"],),
    ).fetchall()
    return FixSessionSnapshot(
        project_id=row["project_id"],
        environment=row["environment"],
        session_key=row["session_key"],
        begin_string=row["begin_string"],
        sender_comp_id=row["sender_comp_id"],
        target_comp_id=row["target_comp_id"],
        heartbeat_seconds=row["heartbeat_seconds"],
        reset_seq_num_on_logon=(bool(row["reset_seq_num_on_logon"]) if row["reset_seq_num_on_logon"] is not None else None),
        account_reference=row["account_reference"],
        username_secret_reference=row["username_secret_reference"],
        password_secret_reference=row["password_secret_reference"],
        endpoints=[FixSessionEndpoint(**dict(ep)) for ep in endpoints],
        metadata=_loads(row["metadata_json"], {}),
    )


def list_sessions(conn: sqlite3.Connection, project_id: str, environment: str | None = None) -> list[dict[str, Any]]:
    params: list[Any] = [project_id]
    env_clause = ""
    if environment:
        env_clause = " AND s.environment=?"
        params.append(environment.upper())
    rows = conn.execute(
        f"""SELECT s.* FROM project_fix_sessions s
            WHERE s.project_id=? AND s.is_current=1 {env_clause}
            ORDER BY s.environment, s.session_key""",
        params,
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["metadata"] = _loads(item.pop("metadata_json", "{}"), {})
        item["endpoints"] = [
            dict(ep)
            for ep in conn.execute(
                """SELECT route_role, host, port, priority, transport
                   FROM fix_session_endpoints WHERE session_version_id=?
                   ORDER BY priority, endpoint_id""",
                (row["session_version_id"],),
            ).fetchall()
        ]
        item["has_username_secret"] = bool(item.pop("username_secret_reference", None))
        item["has_password_secret"] = bool(item.pop("password_secret_reference", None))
        result.append(item)
    return result


def list_history(conn: sqlite3.Connection, project_id: str, environment: str, session_key: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT session_version_id, project_id, environment, session_key, version_no,
                  is_current, begin_string, sender_comp_id, target_comp_id,
                  heartbeat_seconds, reset_seq_num_on_logon, account_reference,
                  confirmation_mode, confirmed_by, confirmed_at, superseded_at,
                  source_candidate_id
           FROM project_fix_sessions
           WHERE project_id=? AND environment=? AND session_key=?
           ORDER BY version_no DESC""",
        (project_id, environment.upper(), session_key),
    ).fetchall()
    return [dict(row) for row in rows]


def create_candidate(conn: sqlite3.Connection, candidate: FixSessionCandidate, idempotency_key: str | None = None) -> str:
    payload = candidate.snapshot.model_dump(mode="json")
    diff = {
        "changed_fields": candidate.changed_fields,
        "inherited_fields": candidate.inherited_fields,
    }
    cursor = conn.execute(
        """INSERT OR IGNORE INTO fix_session_config_candidates
           (candidate_id, project_id, environment, session_key, proposed_snapshot_json,
            diff_json, source_email_id, source_attachment_id, evidence_id, sender_email,
            sender_policy_id, spf_result, dkim_result, dmarc_result, aligned_sender,
            confidence, completeness, changed_fields_json, inherited_fields_json,
            conflict_reasons_json, field_sources_json, status, received_at, idempotency_key)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            candidate.candidate_id,
            candidate.snapshot.project_id,
            candidate.snapshot.environment.value,
            candidate.snapshot.session_key,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            json.dumps(diff, ensure_ascii=False, sort_keys=True),
            candidate.source_email_id,
            candidate.source_attachment_id,
            candidate.evidence_id,
            candidate.sender_email,
            candidate.sender_policy_id,
            candidate.authentication.spf.value,
            candidate.authentication.dkim.value,
            candidate.authentication.dmarc.value,
            int(candidate.authentication.aligned_sender),
            candidate.confidence,
            candidate.completeness,
            json.dumps(candidate.changed_fields),
            json.dumps(candidate.inherited_fields),
            json.dumps(candidate.conflict_reasons),
            json.dumps(candidate.field_sources, ensure_ascii=False, sort_keys=True),
            candidate.status.value,
            candidate.received_at,
            idempotency_key,
        ),
    )
    if cursor.rowcount == 0 and idempotency_key:
        row = conn.execute(
            "SELECT candidate_id FROM fix_session_config_candidates WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if row:
            return row["candidate_id"]
    return candidate.candidate_id


def get_candidate(conn: sqlite3.Connection, candidate_id: str) -> FixSessionCandidate | None:
    row = conn.execute(
        "SELECT * FROM fix_session_config_candidates WHERE candidate_id=?",
        (candidate_id,),
    ).fetchone()
    if not row:
        return None
    return FixSessionCandidate(
        candidate_id=row["candidate_id"],
        snapshot=FixSessionSnapshot(**_loads(row["proposed_snapshot_json"], {})),
        source_email_id=row["source_email_id"],
        source_attachment_id=row["source_attachment_id"],
        evidence_id=row["evidence_id"],
        sender_email=row["sender_email"],
        sender_policy_id=row["sender_policy_id"],
        authentication=EmailAuthentication(
            spf=row["spf_result"], dkim=row["dkim_result"], dmarc=row["dmarc_result"],
            aligned_sender=bool(row["aligned_sender"]),
        ),
        confidence=row["confidence"],
        completeness=row["completeness"],
        changed_fields=_loads(row["changed_fields_json"], []),
        inherited_fields=_loads(row["inherited_fields_json"], []),
        conflict_reasons=_loads(row["conflict_reasons_json"], []),
        field_sources=_loads(row["field_sources_json"], {}),
        status=row["status"],
        received_at=row["received_at"],
    )


def list_candidates(conn: sqlite3.Connection, project_id: str | None = None, status: str = "pending_review") -> list[dict[str, Any]]:
    clauses = ["status=?"]
    params: list[Any] = [status]
    if project_id:
        clauses.append("project_id=?")
        params.append(project_id)
    rows = conn.execute(
        f"""SELECT candidate_id, project_id, environment, session_key, sender_email,
                   confidence, completeness, conflict_reasons_json, changed_fields_json,
                   inherited_fields_json, source_email_id, received_at, created_at
            FROM fix_session_config_candidates
            WHERE {' AND '.join(clauses)} ORDER BY created_at DESC""",
        params,
    ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["conflict_reasons"] = _loads(item.pop("conflict_reasons_json"), [])
        item["changed_fields"] = _loads(item.pop("changed_fields_json"), [])
        item["inherited_fields"] = _loads(item.pop("inherited_fields_json"), [])
        items.append(item)
    return items


def confirm_candidate(
    conn: sqlite3.Connection,
    candidate: FixSessionCandidate,
    *,
    mode: ConfirmationMode,
    confirmed_by: str,
    reason: str,
) -> str:
    snapshot = candidate.snapshot
    current = conn.execute(
        """SELECT session_version_id, version_no FROM project_fix_sessions
           WHERE project_id=? AND environment=? AND session_key=? AND is_current=1""",
        (snapshot.project_id, snapshot.environment.value, snapshot.session_key),
    ).fetchone()
    version_no = int(current["version_no"]) + 1 if current else 1
    now = _utc_now()
    if current:
        conn.execute(
            """UPDATE project_fix_sessions SET is_current=0, superseded_at=?
               WHERE session_version_id=? AND is_current=1""",
            (now, current["session_version_id"]),
        )
    version_id = "fsv_" + uuid.uuid4().hex
    conn.execute(
        """INSERT INTO project_fix_sessions
           (session_version_id, project_id, environment, session_key, version_no,
            is_current, begin_string, sender_comp_id, target_comp_id, heartbeat_seconds,
            reset_seq_num_on_logon, account_reference, username_secret_reference,
            password_secret_reference, metadata_json, source_candidate_id,
            confirmation_mode, confirmed_by, confirmed_at)
           VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            version_id, snapshot.project_id, snapshot.environment.value, snapshot.session_key,
            version_no, snapshot.begin_string, snapshot.sender_comp_id, snapshot.target_comp_id,
            snapshot.heartbeat_seconds,
            int(snapshot.reset_seq_num_on_logon) if snapshot.reset_seq_num_on_logon is not None else None,
            snapshot.account_reference, snapshot.username_secret_reference,
            snapshot.password_secret_reference, json.dumps(snapshot.metadata, sort_keys=True),
            candidate.candidate_id, mode.value, confirmed_by, now,
        ),
    )
    for endpoint in snapshot.endpoints:
        conn.execute(
            """INSERT INTO fix_session_endpoints
               (session_version_id, route_role, host, port, priority, transport)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (version_id, endpoint.route_role.value, endpoint.host, endpoint.port, endpoint.priority, endpoint.transport),
        )
    inherited_from = current["session_version_id"] if current else None
    for field_path, source in candidate.field_sources.items():
        value = _value_for_path(snapshot, field_path)
        conn.execute(
            """INSERT INTO fix_session_field_sources
               (session_version_id, field_path, source_email_id, source_attachment_id,
                evidence_id, source_location, extraction_method,
                inherited_from_version_id, value_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                version_id, field_path, source.get("source_email_id") or candidate.source_email_id,
                source.get("source_attachment_id") or candidate.source_attachment_id,
                source.get("evidence_id") or candidate.evidence_id,
                source.get("source_location"), source.get("extraction_method", "deterministic"),
                inherited_from if field_path in candidate.inherited_fields else source.get("inherited_from_version_id"),
                hashlib.sha256(str(value).encode("utf-8")).hexdigest(),
            ),
        )
    conn.execute(
        """UPDATE fix_session_config_candidates
           SET status='confirmed', decision_mode=?, decision_reason=?, decided_by=?, decided_at=?
           WHERE candidate_id=? AND status='pending_review'""",
        (mode.value, reason, confirmed_by, now, candidate.candidate_id),
    )
    return version_id


def _value_for_path(snapshot: FixSessionSnapshot, path: str) -> Any:
    data = snapshot.model_dump(mode="json")
    current: Any = data
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def list_environment_resources(conn: sqlite3.Connection, project_id: str, environment: str | None = None) -> list[dict[str, Any]]:
    clauses = ["project_id=?", "is_current=1"]
    params: list[Any] = [project_id]
    if environment:
        clauses.append("environment=?")
        params.append(environment.upper())
    rows = conn.execute(
        f"""SELECT resource_id, project_id, market, environment, resource_type,
                   label, url, value_text, secret_reference, distribution_class,
                   metadata_json, effective_from, effective_to, approved_by, approved_at
            FROM fix_environment_resources WHERE {' AND '.join(clauses)}
            ORDER BY environment, resource_type, label""",
        params,
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["metadata"] = _loads(item.pop("metadata_json"), {})
        if item.get("distribution_class") == "secret":
            item["value_text"] = None
        result.append(item)
    return result
