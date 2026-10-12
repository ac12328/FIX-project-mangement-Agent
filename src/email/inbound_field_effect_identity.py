"""Identity of actual A field effects in a caller-owned application transaction.

No extra operation or proposal is created. Direct writes already have an accepted
safe-write audit and activity; auto-apply additionally has an accepted proposal,
undo memory and delivery intent. Export only freshly observed real identities.
"""
from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

FIELD_EFFECT_OUTCOMES = frozenset({
    "direct_write", "auto_applied_undo_card", "auto_applied_notification_pending",
    "auto_applied_notification_unknown",
})


class ProjectFieldEffectCommandReceipt(BaseModel):
    """A local effect receipt, never a commit or delivery acknowledgement."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    project_id: str = Field(min_length=1)
    source_ref: str = Field(min_length=1)
    source_type: Literal["email"] = "email"
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    write_action: Literal["direct_write", "auto_applied_notification_pending"]
    status: Literal["applied_in_application_transaction"] = "applied_in_application_transaction"
    evidence_id: str = Field(min_length=1)
    applied_fields: dict[str, str | None] = Field(min_length=1)
    audit_update_id: str = Field(min_length=1)
    activity_id: int = Field(gt=0)
    update_id: str | None = Field(default=None, min_length=1)
    delivery_intent_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _validate_optional_identity(self):
        if self.write_action == "direct_write":
            if self.update_id is not None or self.delivery_intent_id is not None:
                raise ValueError("Direct field effect has no undo proposal or delivery intent")
        elif (not self.update_id or self.delivery_intent_id != self.update_id
              or self.update_id == self.audit_update_id):
            raise ValueError("Auto effect requires its distinct actual proposal and matching intent")
        return self


def snapshot_field_effect_ids(conn, project_id):
    """Pin real preexisting IDs so the exporter cannot reuse a prior effect."""
    if not conn.in_transaction:
        raise ValueError("Claimed field effects require the caller's active transaction")
    audits = {row[0] for row in conn.execute(
        "SELECT update_id FROM proposed_updates WHERE project_id=? AND task_type='direct_write'",
        (project_id,),
    )}
    activities = {row[0] for row in conn.execute(
        "SELECT id FROM activity_log WHERE project_id=? AND action_type='auto_truth_write'",
        (project_id,),
    )}
    return audits, activities


def _source_identity(conn, project_id, source_ref, evidence_id):
    from config.defaults import EMAIL_BODY_SNIPPET_CHARS

    source = conn.execute(
        "SELECT project_id,sender_email,body_snippet FROM processed_emails WHERE email_id=?",
        (source_ref,),
    ).fetchone()
    evidence = conn.execute(
        "SELECT project_id,source_ref,content_text FROM evidence_store WHERE evidence_id=?",
        (evidence_id,),
    ).fetchone()
    if source is None or source[0] != project_id or evidence is None or tuple(evidence) != (
        project_id, source_ref, (source[2] or "")[:EMAIL_BODY_SNIPPET_CHARS],
    ):
        raise ValueError("Field effect evidence must match its persisted email source")
    return hashlib.sha256(((source[1] or "") + "\0" + (source[2] or "")).encode("utf-8")).hexdigest()


def export_field_effect_command(conn, *, before, project_id, source_ref, evidence_ref,
                                proposed_fields, outcome):
    audits, activities = snapshot_field_effect_ids(conn, project_id)
    new_audits, new_activities = audits - before[0], activities - before[1]
    if len(new_audits) != 1 or len(new_activities) != 1:
        raise ValueError("Claimed field effect requires one freshly persisted audit and activity")
    audit_id, activity_id = next(iter(new_audits)), next(iter(new_activities))
    changes = json.loads(conn.execute(
        "SELECT proposed_changes FROM proposed_updates WHERE update_id=?", (audit_id,),
    ).fetchone()[0])
    actual_fields = {c["field"]: (str(proposed_fields[c["field"]])
                     if proposed_fields[c["field"]] is not None else None) for c in changes}
    activity = conn.execute("SELECT object_ref FROM activity_log WHERE id=?", (activity_id,)).fetchone()
    update_id = activity[0] if outcome != "direct_write" else None
    command = ProjectFieldEffectCommandReceipt(
        project_id=project_id, source_ref=source_ref,
        source_hash=_source_identity(conn, project_id, source_ref, evidence_ref),
        write_action=outcome, evidence_id=evidence_ref, applied_fields=actual_fields,
        audit_update_id=audit_id, activity_id=activity_id, update_id=update_id,
        delivery_intent_id=update_id,
    )
    # The legacy direct activity has no object_ref; bind the freshly written
    # actual activity to its real audit/source. This is durable correlation,
    # not a synthetic pending operation or a caller-commit acknowledgement.
    row = conn.execute("SELECT metadata_json FROM activity_log WHERE id=?", (activity_id,)).fetchone()
    metadata = json.loads(row[0]) if row[0] else {}
    if "inbound_field_effect" in metadata:
        raise ValueError("Fresh field activity already carries an effect identity")
    metadata["inbound_field_effect"] = _effect_link(command)
    conn.execute("UPDATE activity_log SET metadata_json=? WHERE id=?",
                 (json.dumps(metadata, sort_keys=True), activity_id))
    return validate_field_effect_command(
        conn, command, outcome=outcome, project_id=project_id, source_ref=source_ref,
        evidence_ref=evidence_ref,
    )


def _effect_link(command):
    return {name: getattr(command, name) for name in (
        "project_id", "source_ref", "source_hash", "evidence_id", "audit_update_id",
        "applied_fields", "write_action", "update_id", "delivery_intent_id",
    )}


def _validate_changes(changes, command, *, audit):
    if (not changes or len({c["field"] for c in changes}) != len(changes)
            or {c["evidence_ref"] for c in changes} != {command.evidence_id}):
        raise ValueError("Field effect requires distinct real evidence-bound audit changes")
    expected = {k: (v if v is not None else "") for k, v in command.applied_fields.items()} if audit else command.applied_fields
    if {c["field"]: c["new_value"] for c in changes} != expected:
        raise ValueError("Field effect changes must match the exported actual effect")


def validate_field_effect_command(conn, command, *, outcome, project_id, source_ref,
                                  evidence_ref, **_):
    """Re-correlate actual persisted source, truth, audit, activity and optional undo/outbox."""
    command = ProjectFieldEffectCommandReceipt.model_validate(command.model_dump())
    if (not conn.in_transaction or command.project_id != project_id
            or command.source_ref != source_ref or command.evidence_id != evidence_ref
            or command.write_action != outcome):
        raise ValueError("Field effect identity must match its caller's source-bound outcome")
    if command.source_hash != _source_identity(conn, project_id, source_ref, evidence_ref):
        raise ValueError("Field effect content hash must match its persisted email source")
    audit = conn.execute(
        "SELECT project_id,task_type,decision_outcome,proposed_changes,operation_id "
        "FROM proposed_updates WHERE update_id=?", (command.audit_update_id,),
    ).fetchone()
    activity = conn.execute(
        "SELECT project_id,action_type,object_ref,surface,metadata_json FROM activity_log WHERE id=?",
        (command.activity_id,),
    ).fetchone()
    if (audit is None or tuple(audit[:3]) != (project_id, "direct_write", "accepted")
            or audit[4] is not None or activity is None
            or tuple(activity[:4]) != (project_id, "auto_truth_write", command.update_id, "email")
            or json.loads(activity[4] or "{}").get("inbound_field_effect") != _effect_link(command)):
        raise ValueError("Field effect requires its actual accepted audit and email activity")
    changes = json.loads(audit[3])
    _validate_changes(changes, command, audit=True)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(projects)")}
    for name, value in command.applied_fields.items():
        if name not in columns:
            raise ValueError("Field effect references a nonexistent project field")
        row = conn.execute(f'SELECT "{name}" FROM projects WHERE project_id=?', (project_id,)).fetchone()
        if row is None or (str(row[0]) if row[0] is not None else None) != value:
            raise ValueError("Field effect does not match current project truth")
    if command.update_id is None:
        return command
    proposal = conn.execute(
        "SELECT project_id,decision_outcome,operation_id,proposed_changes FROM proposed_updates WHERE update_id=?",
        (command.update_id,),
    ).fetchone()
    delivery = conn.execute(
        "SELECT project_id,state,attempts,payload_json FROM email_update_notification_outbox WHERE update_id=?",
        (command.delivery_intent_id,),
    ).fetchone()
    memory = conn.execute(
        "SELECT memory_value FROM working_memory WHERE project_id=? AND memory_key=?",
        (project_id, "undo_auto_apply:" + command.update_id),
    ).fetchone()
    if (proposal is None or tuple(proposal[:3]) != (project_id, "accepted", None)
            or delivery is None or tuple(delivery[:3]) != (project_id, "pending", 0)
            or memory is None):
        raise ValueError("Auto effect requires its actual accepted proposal, unsent intent and undo")
    _validate_changes(json.loads(proposal[3]), command, audit=False)
    payload, undo = json.loads(delivery[3]), json.loads(memory[0])
    if (payload.get("project_id") != project_id or payload.get("update_id") != command.update_id
            or payload.get("evidence_refs") != [command.evidence_id]
            or set(payload.get("applied_fields", [])) != set(command.applied_fields)
            or {c["field"]: c["new_value"] for c in payload.get("proposed_changes", [])} != command.applied_fields
            or set(undo) != set(command.applied_fields)
            or {k: (str(v["new_value"]) if v["new_value"] is not None else None)
                for k, v in undo.items()} != command.applied_fields):
        raise ValueError("Auto effect intent and undo must match its actual field changes")
    for change in changes:
        old = undo[change["field"]]["old_value"]
        if change["old_value"] != (str(old) if old is not None else ""):
            raise ValueError("Auto effect undo revision must match its real pre-write audit")
    return command
