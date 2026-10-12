"""Source-bound action-bundle identity; never an owner COMMIT acknowledgement."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.email.body_cleaner import clean_email_body
from src.email.source_authority import normalized_sender
from src.models.followup_plan import ActionBundle


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError("Action-bundle receipt " + reason)


class ActionBundleCommandReceipt(BaseModel):
    """Correlate one actual bundle to its source and retained effects.

    The applied status describes the application transaction, not acknowledged
    durability. An existing bundle may precede the caller's application receipt.
    Reopen validation checks retained identity, not today's mutable plan summary.
    """
    model_config = ConfigDict(frozen=True, extra="forbid")

    project_id: str = Field(min_length=1)
    source_ref: str = Field(min_length=1)
    source_hash: str = Field(min_length=64, max_length=64)
    operation_id: str = Field(min_length=1)
    actor_id: str = Field(min_length=1)
    bundle_content_hash: str = Field(min_length=1)
    source_evidence_id: str = Field(min_length=1)
    bundle_evidence_id: str = Field(min_length=1)
    action_ids: dict[str, str] = Field(min_length=1)
    action_event_ids: list[str] = Field(min_length=1)
    plan_revision: int = Field(ge=1)
    applied_fields: dict[str, str]
    status: Literal["applied_in_application_transaction", "already_applied"]

    @model_validator(mode="after")
    def _validate_identity(self):
        expected = "email-actions-" + _hash(self.project_id + "\0" + self.source_ref)[:32]
        _require(self.operation_id == expected == self.source_evidence_id,
                 "operation does not match project/source")
        _require(all(self.action_ids) and all(self.action_ids.values()), "has empty action IDs")
        _require(len(set(self.action_ids.values())) == len(self.action_ids), "has duplicate effect IDs")
        _require(len(self.action_event_ids) == len(set(self.action_event_ids)) == len(self.action_ids),
                 "event identities do not match action count")
        return self

    def validate_in(self, conn: sqlite3.Connection) -> None:
        """Verify real rows in the current transaction or an independent reopen."""
        # Revalidate nested mutable containers even on model_copy/construct inputs.
        self._validate_identity()
        source = conn.execute("""SELECT project_id,sender_email,body_snippet
            FROM processed_emails WHERE email_id=?""", (self.source_ref,)).fetchone()
        _require(source is not None and source[0] == self.project_id, "source/project missing or mismatched")
        body = clean_email_body(source[2] or "")
        _require(self.actor_id == "email:" + normalized_sender(source[1]), "source actor mismatch")
        _require(_hash(body) == self.source_hash, "source content mismatch")
        evidence = conn.execute("""SELECT project_id,source_ref,content_text FROM evidence_store
            WHERE evidence_id=?""", (self.source_evidence_id,)).fetchone()
        _require(evidence is not None and tuple(evidence) == (self.project_id,self.source_ref,body),
                 "source evidence mismatch")
        durable = conn.execute("""SELECT actor_id,content_hash,result_json FROM followup_bundle_receipts
            WHERE project_id=? AND operation_id=?""", (self.project_id,self.operation_id)).fetchone()
        _require(durable is not None, "bundle row missing")
        _require(durable[0] == self.actor_id and durable[1] == self.bundle_content_hash,
                 "bundle actor/content mismatch")
        result = json.loads(durable[2])
        _require(result.get("outcome") == "applied" and result.get("operation_id") == self.operation_id,
                 "bundle operation outcome mismatch")
        _require(result.get("action_ids") == self.action_ids and result.get("plan_revision") == self.plan_revision
                 and result.get("applied_fields") == self.applied_fields, "bundle effect/revision mismatch")
        events = conn.execute("""SELECT event_id,followup_id,actor_id,operation,evidence_ref
            FROM followup_action_events WHERE project_id=? AND operation_id=? ORDER BY event_id""",
            (self.project_id,self.operation_id)).fetchall()
        _require([e[0] for e in events] == sorted(self.action_event_ids), "event IDs missing or mismatched")
        _require(sorted(e[1] for e in events) == sorted(self.action_ids.values()), "event action effects mismatch")
        _require(all(e[2] == self.actor_id and e[4] == self.bundle_evidence_id for e in events),
                 "event actor/evidence mismatch")
        bundle_evidence = conn.execute("""SELECT project_id,source_ref,content_text FROM evidence_store
            WHERE evidence_id=?""", (self.bundle_evidence_id,)).fetchone()
        _require(bundle_evidence is not None and bundle_evidence[0] == self.project_id
                 and bundle_evidence[1] == "followup-bundle:" + self.operation_id, "bundle evidence mismatch")
        bundle = ActionBundle.model_validate_json(bundle_evidence[2])
        _require(bundle.operation_id == self.operation_id and bundle.evidence_ref == self.source_evidence_id,
                 "bundle source binding mismatch")
        if self.bundle_content_hash.startswith("fields-v2:"):
            expected_hash = "fields-v2:" + _hash(bundle.identity_json())
        else:
            # The service already rejects ambiguous legacy explicit-null updates.
            ambiguous = any(a.operation == "update" and any(getattr(a,f) is None for f in
                ("description","owner_side","due_date","reminder_due_at","depends_on")) for a in bundle.actions)
            _require(not ambiguous, "legacy field-presence identity needs reconciliation")
            expected_hash = _hash(bundle.model_dump_json())
        _require(expected_hash == self.bundle_content_hash, "bundle evidence content hash mismatch")
        _require(self.plan_revision == bundle.expected_plan_revision + 1, "bundle plan revision mismatch")
        expected_events = sorted((self.action_ids[a.action_id],a.operation) for a in bundle.actions)
        _require(sorted((e[1],e[3]) for e in events) == expected_events, "bundle action/event intent mismatch")
        for effect in self.action_ids.values():
            action = conn.execute("SELECT project_id FROM internal_followups WHERE followup_id=?", (effect,)).fetchone()
            _require(action is not None and action[0] == self.project_id, "action effect missing or wrong project")
        head = conn.execute("SELECT revision FROM followup_plan_heads WHERE project_id=?", (self.project_id,)).fetchone()
        _require(head is not None and head[0] >= self.plan_revision, "plan head precedes receipt")


def action_bundle_command_receipt(conn: sqlite3.Connection, *, project_id: str,
    source_ref: str, bundle: ActionBundle, actor_id: str, result: dict,
) -> ActionBundleCommandReceipt:
    """Build only from the actual bridge result and persisted bundle/event rows."""
    durable = conn.execute("""SELECT content_hash FROM followup_bundle_receipts
        WHERE project_id=? AND operation_id=?""", (project_id,bundle.operation_id)).fetchone()
    _require(durable is not None, "bundle result has no stored identity")
    events = conn.execute("""SELECT event_id,evidence_ref FROM followup_action_events
        WHERE project_id=? AND operation_id=? ORDER BY event_id""", (project_id,bundle.operation_id)).fetchall()
    _require(bool(events) and len({e[1] for e in events}) == 1, "bundle result has no unique event evidence")
    source = conn.execute("SELECT body_snippet FROM processed_emails WHERE email_id=?", (source_ref,)).fetchone()
    _require(source is not None, "source missing")
    _require(result.get("outcome") in {"applied","duplicate"}, "unexpected result outcome")
    command = ActionBundleCommandReceipt(
        project_id=project_id,source_ref=source_ref,
        source_hash=_hash(clean_email_body(source[0] or "")),
        operation_id=result["operation_id"],actor_id=actor_id,
        bundle_content_hash=durable[0],source_evidence_id=bundle.evidence_ref,
        bundle_evidence_id=events[0][1],action_ids=result["action_ids"],
        action_event_ids=[e[0] for e in events],plan_revision=result["plan_revision"],
        applied_fields=result["applied_fields"],
        status="applied_in_application_transaction" if result["outcome"] == "applied" else "already_applied",
    )
    command.validate_in(conn)
    return command
