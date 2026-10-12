"""Capture and claim inbound source work without replaying uncertain effects.

This ledger is intentionally not a second routing engine. Domain-command checkpoints
and operator resolution must be added before automatic route recovery is enabled.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from typing import Literal

from src.email.inbox_fetcher import FetchedEmail
from src.email.action_bundle_receipt import ActionBundleCommandReceipt
from src.triage.email_triage import ClassifiedEvent
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.email.inbound_field_effect_identity import (
    FIELD_EFFECT_OUTCOMES, ProjectFieldEffectCommandReceipt,
)


class RouteDeliveryIntent(BaseModel):
    """Recipient and content identity, committed before an external card send."""
    model_config = ConfigDict(frozen=True)

    chat_id: int
    content_sha256: str

    @classmethod
    def for_message(cls, chat_id: int, text: str) -> "RouteDeliveryIntent":
        if not chat_id or not text:
            raise ValueError("Card delivery requires a recipient and content")
        return cls(chat_id=chat_id, content_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest())


class ReplyCommandReceipt(BaseModel):
    """Committed reply identity and review/write outcome for route reconciliation."""
    model_config = ConfigDict(frozen=True)

    reply_id: str
    write_action: str


class InboundRouteClaim(BaseModel):
    """Lease-bound source identity for a scheduled inbound command."""
    model_config = ConfigDict(frozen=True)

    email_id: str = Field(min_length=1)
    token: str = Field(min_length=1)


class ProjectStateCommandReceipt(BaseModel):
    """State effect IDs in the same durable application checkpoint.

    The applied status describes a write inside this application transaction;
    the checkpoint is observable only after the enclosing commit. It is not
    evidence that an inner router committed independently.
    """
    model_config = ConfigDict(frozen=True, extra="forbid")

    project_id: str = Field(min_length=1)
    source_ref: str = Field(min_length=1)
    write_action: str = Field(min_length=1)
    status: Literal["applied_in_application_transaction", "not_applied", "incomplete"]
    proposed_fields: dict[str, str] = Field(default_factory=dict)
    source_type: Literal["email"] = "email"
    evidence_id: str | None = None
    review_id: str | None = None
    update_id: str | None = None

    @model_validator(mode="after")
    def _require_attributable_effect(self):
        if self.status == "applied_in_application_transaction" and (
            not self.evidence_id or not self.update_id
        ):
            raise ValueError("Applied state effect requires evidence and update IDs")
        return self


class ProjectLagReviewCommandReceipt(BaseModel):
    """Review proposal in the caller-owned application, not a milestone change."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    project_id: str = Field(min_length=1)
    source_ref: str = Field(min_length=1)
    source_type: Literal["email"] = "email"
    preview_result: str = Field(min_length=1)
    status: Literal["proposed_in_application_transaction", "not_applied", "incomplete"]
    proposed_update_id: str | None = None
    proposed_fields: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _require_proposal_identity(self):
        if self.status == "proposed_in_application_transaction":
            if self.preview_result != "needs_review" or not self.proposed_update_id:
                raise ValueError("Applied lag preview requires a review result and proposal ID")
        elif self.proposed_update_id is not None:
            raise ValueError("Unapplied lag preview cannot claim a proposal ID")
        return self


class ProjectFieldCommandReceipt(ProjectStateCommandReceipt):
    """A source-bound B field effect recorded with its application transaction."""


FIELD_REVIEW_OUTCOMES = frozenset({
    "review_card", "review_notification_pending", "review_notification_unknown",
})


class ProjectFieldReviewCommandReceipt(BaseModel):
    """Actual review proposal and delivery intent, not an applied truth write.

    Source hash covers the persisted sender and captured body snippet. The
    proposal/outbox share update_id; this does not invent a typed operation ID
    or assert that the caller has committed or delivered a Telegram card.
    """
    model_config = ConfigDict(frozen=True, extra="forbid")

    project_id: str = Field(min_length=1)
    source_ref: str = Field(min_length=1)
    source_type: Literal["email"] = "email"
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    write_action: Literal["review_card", "review_notification_pending", "review_notification_unknown"]
    status: Literal["proposed_in_application_transaction"] = "proposed_in_application_transaction"
    evidence_id: str = Field(min_length=1)
    update_id: str = Field(min_length=1)
    delivery_intent_id: str = Field(min_length=1)
    proposed_fields: dict[str, str | None] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_delivery_identity(self):
        if self.delivery_intent_id != self.update_id:
            raise ValueError("Field review delivery intent must match its real proposal ID")
        return self


class FollowupIntentReceipt(BaseModel):
    """One source-bound intent observed inside the caller-owned application transaction."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    project_id: str = Field(min_length=1)
    source_ref: str = Field(min_length=1)
    description: str = Field(min_length=1)
    due_date: str | None = None
    followup_id: str | None = None
    status: Literal[
        "created_in_application_transaction",
        "existing_in_application_transaction",
        "not_applied",
    ]
    reason: Literal["missing_stored_analysis", "application_incomplete"] | None = None

    @model_validator(mode="after")
    def _validate_outcome(self):
        if self.status == "not_applied":
            if self.followup_id is not None or self.reason is None:
                raise ValueError("Unapplied follow-up requires a reason and no effect ID")
        elif not self.followup_id or self.reason is not None:
            raise ValueError("Applied or existing follow-up requires an ID and no reason")
        return self


class PctCompletionCommandReceipt(BaseModel):
    """Identity of the nested PCT closure, durable with the B application step."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    project_id: str = Field(min_length=1)
    receipt_id: str = Field(min_length=1)  # pct_completion_receipts.project_id.
    source_ref: str = Field(min_length=1)
    source_hash: str = Field(min_length=1)
    status: Literal["applied_in_application_transaction", "already_completed"]
    evidence_id: str | None = None
    update_id: str | None = None
    ticket_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_identity(self):
        if self.receipt_id != self.project_id:
            raise ValueError("PCT receipt ID must match its project primary key")
        if self.status == "applied_in_application_transaction" and (
            not self.evidence_id or not self.update_id
        ):
            raise ValueError("Applied PCT completion requires evidence and update IDs")
        if len(set(self.ticket_ids)) != len(self.ticket_ids):
            raise ValueError("PCT handoff IDs must be distinct")
        return self


class ProjectApplicationReceipt(BaseModel):
    """One A/B application result, not a receipt for every nested command."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    project_id: str = Field(min_length=1)
    source_ref: str = Field(min_length=1)
    status: str = Field(min_length=1)
    write_action: str = Field(min_length=1)
    state_write_action: str | None = None
    state_command: ProjectStateCommandReceipt | None = None
    pct_command: PctCompletionCommandReceipt | None = None
    field_command: ProjectFieldCommandReceipt | None = None
    field_review_command: ProjectFieldReviewCommandReceipt | None = None
    field_effect_command: ProjectFieldEffectCommandReceipt | None = None
    lag_review_command: ProjectLagReviewCommandReceipt | None = None
    followup_commands: list[FollowupIntentReceipt] = Field(default_factory=list)
    action_command: ActionBundleCommandReceipt | None = None
    action_review_id: str | None = None
    requires_reconciliation: bool

    @model_validator(mode="after")
    def _validate_state_command(self):
        action = self.action_command
        action_status = {"email_action_bundle_applied": "applied_in_application_transaction",
                         "email_action_bundle_duplicate": "already_applied"}.get(self.write_action)
        if action_status is not None:
            if action is None or action.project_id != self.project_id or action.source_ref != self.source_ref or action.status != action_status:
                raise ValueError("Action command must match application source/project/outcome")
        elif action is not None:
            raise ValueError("Action command requires an applied/duplicate bundle outcome")
        pct = self.pct_command
        if pct is not None:
            if pct.project_id != self.project_id or pct.source_ref != self.source_ref:
                raise ValueError("PCT command must match application source and project")
            if pct.status == "applied_in_application_transaction":
                if self.write_action in {"review_persistence_failed", "field_route_failed"}:
                    # An independent PCT survives a failed speculative sibling,
                    # but that failure must remain incomplete and reconcilable.
                    if self.status != "failed" or not self.requires_reconciliation:
                        raise ValueError("Failed PCT sibling requires a failed, reconcilable application")
                elif self.status != "completed":
                    raise ValueError("Applied PCT command requires a completed application")
        if self.write_action in {"pct_completed", "pct_already_completed"} and pct is None:
            raise ValueError("PCT application requires its nested completion identity")
        command = self.state_command
        if command is not None:
            if command.project_id != self.project_id or command.source_ref != self.source_ref:
                raise ValueError("State command must match application source and project")
            if command.write_action != self.state_write_action:
                raise ValueError("State command must match application state outcome")
            if command.status == "incomplete" and not self.requires_reconciliation:
                raise ValueError("Incomplete state command requires reconciliation")
        field_command = self.field_command
        if field_command is not None:
            if (field_command.project_id != self.project_id
                    or field_command.source_ref != self.source_ref):
                raise ValueError("Field command must match application source and project")
            if field_command.write_action != self.write_action:
                raise ValueError("Field command must match application field outcome")
            if field_command.status == "incomplete" and not self.requires_reconciliation:
                raise ValueError("Incomplete field command requires reconciliation")
        effect = self.field_effect_command
        if effect is not None:
            if effect.project_id != self.project_id or effect.source_ref != self.source_ref:
                raise ValueError("Field effect must match application source and project")
            if self.write_action == "field_route_failed":
                if self.status != "failed" or not self.requires_reconciliation:
                    raise ValueError("Retained field effect requires a failed, reconcilable application")
            elif effect.write_action != self.write_action or self.status != "completed" or self.requires_reconciliation:
                raise ValueError("Field effect must match a completed application outcome")
        if self.write_action in FIELD_EFFECT_OUTCOMES and effect is None and field_command is None:
            raise ValueError("Applied field outcome requires its nested effect command identity")
        if effect is not None and self.field_review_command is not None:
            raise ValueError("One regular field command cannot be both proposed and applied")
        review = self.field_review_command
        if review is not None:
            if review.project_id != self.project_id or review.source_ref != self.source_ref:
                raise ValueError("Field review must match application source and project")
            if self.write_action == "field_route_failed":
                # The established partial-A contract retains a real review whose
                # later field outcome is uncertain. Keep its identity, but never
                # certify complete success or remove reconciliation.
                if self.status != "failed" or not self.requires_reconciliation:
                    raise ValueError("Retained partial review requires a failed, reconcilable application")
            elif review.write_action != self.write_action or self.status != "completed":
                raise ValueError("Field review must match a completed review application outcome")
        if (self.write_action in FIELD_REVIEW_OUTCOMES
                and review is None and field_command is None):
            raise ValueError("Review application requires its nested field command identity")
        lag = self.lag_review_command
        if lag is not None:
            if lag.project_id != self.project_id or lag.source_ref != self.source_ref:
                raise ValueError("Lag review must match application source and project")
            if lag.status == "incomplete" and not self.requires_reconciliation:
                raise ValueError("Incomplete lag review requires reconciliation")
            if lag.status == "proposed_in_application_transaction" and (
                "milestone_review_required" not in {self.write_action, self.state_write_action}
            ):
                raise ValueError("Lag proposal must match a milestone review outcome")
        seen = set()
        for followup in self.followup_commands:
            if followup.project_id != self.project_id or followup.source_ref != self.source_ref:
                raise ValueError("Follow-up command must match application source and project")
            identity = (followup.description, followup.due_date)
            if identity in seen:
                raise ValueError("Duplicate follow-up intent in application receipt")
            seen.add(identity)
            if followup.status == "not_applied" and not self.requires_reconciliation:
                raise ValueError("Unapplied follow-up requires reconciliation")
            if followup.status != "not_applied" and self.status != "completed":
                raise ValueError("Follow-up effect requires completed application")
        return self


@dataclass(frozen=True)
class ImapSource:
    account_key: str
    mailbox: str
    uid_validity: int

    @classmethod
    def from_fetcher(cls, fetcher, uid_validity: int) -> "ImapSource":
        if not isinstance(uid_validity, int) or uid_validity <= 0:
            raise ValueError("IMAP UIDVALIDITY is required for durable routing")
        identity = f"{fetcher.host.lower()}:{fetcher.port}:{fetcher.username.lower()}"
        return cls(hashlib.sha256(identity.encode("utf-8")).hexdigest(),
                   fetcher.inbox_folder, uid_validity)


@dataclass(frozen=True)
class RouteClaimExpectation:
    """Source, classification, and anchor observed before a pending recovery."""
    source_type: str
    account_key: str
    mailbox: str
    uid_validity: int | None
    source_uid: int | None
    classification_json: str
    checkpoint_json: str
    event: ClassifiedEvent

    @classmethod
    def from_pending(cls, row: dict, event: ClassifiedEvent) -> "RouteClaimExpectation":
        return cls(
            source_type=row["source_type"], account_key=row["account_key"],
            mailbox=row["mailbox"], uid_validity=row["uid_validity"],
            source_uid=row["source_uid"],
            classification_json=row["classification_json"],
            checkpoint_json=row["checkpoint_json"], event=event,
        )


def capture_imap_batch(
    conn: sqlite3.Connection,
    items: list[tuple[FetchedEmail, ClassifiedEvent]],
    *,
    source: ImapSource,
) -> list[tuple[FetchedEmail, ClassifiedEvent, bool]]:
    """Atomically persist anchors and pending work; caller owns the commit.

    Duplicate/historical anchors are never converted to pending work: their
    route outcome is unknown. The v123 migration marks historical inbound
    anchors for reconciliation without triggering a replay.
    """
    if not source.account_key or not source.mailbox or source.uid_validity <= 0:
        raise ValueError("A verified IMAP mailbox and UIDVALIDITY are required")
    if any(not isinstance(fetched.uid, int) or fetched.uid <= 0
           for fetched, _ in items):
        raise ValueError("Every captured IMAP email requires a positive UID")
    if any(event.email_id != fetched.email_id for fetched, event in items):
        raise ValueError("IMAP classification email ID must match its source")
    if any(event.sender_email != fetched.sender_email or
           event.subject != fetched.subject for fetched, event in items):
        raise ValueError("IMAP classification metadata must match its source")

    conn.execute("SAVEPOINT capture_imap_work")
    try:
        from src.email.email_processor import persist_classified_email_batch

        persisted = persist_classified_email_batch(conn, items)
        for fetched, event, is_new in persisted:
            if is_new:
                from src.email.source_capture import retain_imap_source

                source_receipt = retain_imap_source(fetched, source)
                conn.execute("""INSERT INTO inbound_email_work
                    (email_id, source_type, account_key, mailbox, uid_validity,
                     source_uid, classification_json, route_state, checkpoint_json)
                    VALUES (?, 'imap', ?, ?, ?, ?, ?, 'pending', ?)""",
                    (fetched.email_id, source.account_key, source.mailbox,
                     source.uid_validity, fetched.uid,
                     json.dumps(asdict(event), ensure_ascii=False),
                     json.dumps({"source_capture": source_receipt.model_dump()}, sort_keys=True)))
            elif conn.execute(
                "SELECT 1 FROM inbound_email_work WHERE email_id=?",
                (fetched.email_id,),
            ).fetchone() is None:
                conn.execute("""INSERT INTO inbound_email_work
                    (email_id, source_type, classification_json, route_state,
                     last_error)
                    VALUES (?, 'legacy', '{}', 'needs_reconciliation',
                            'pre-existing anchor without a route receipt')""",
                    (fetched.email_id,))
        conn.execute("RELEASE SAVEPOINT capture_imap_work")
        return persisted
    except BaseException:
        conn.execute("ROLLBACK TO SAVEPOINT capture_imap_work")
        conn.execute("RELEASE SAVEPOINT capture_imap_work")
        raise


def claim_pending_route(
    conn: sqlite3.Connection, email_id: str,
    expected: RouteClaimExpectation | None = None,
) -> str | None:
    """Atomically claim only the verified pending source and captured anchor.

    Recovery passes its observed snapshot after re-fetching the source. A
    changed source, classification, or anchor cannot be routed from a stale
    observation; callers must reconcile a still-pending mismatch.
    """
    token = uuid.uuid4().hex
    where = "email_id=? AND route_state='pending'"
    args: tuple = (token, email_id)
    if expected is not None:
        if expected.event.email_id != email_id or expected.source_type not in {"imap", "graph"}:
            raise ValueError("Claim expectation must identify the pending source")
        where += """ AND source_type=? AND account_key=? AND mailbox=?
            AND uid_validity IS ? AND source_uid IS ?
            AND classification_json=? AND checkpoint_json=?
            AND EXISTS (SELECT 1 FROM processed_emails AS p
                WHERE p.email_id=inbound_email_work.email_id
                  AND p.sender_email=? AND p.subject=? AND p.result=?
                  AND p.project_id IS ?)"""
        args += (
            expected.source_type, expected.account_key, expected.mailbox,
            expected.uid_validity, expected.source_uid,
            expected.classification_json, expected.checkpoint_json,
            expected.event.sender_email, expected.event.subject,
            expected.event.classification_type, expected.event.project_id,
        )
    row = conn.execute(f"""UPDATE inbound_email_work
        SET route_state='routing', lease_token=?,
            lease_expires_at=datetime('now', '+15 minutes'),
            attempt_count=attempt_count+1, updated_at=datetime('now')
        WHERE {where}
        RETURNING email_id""", args).fetchone()
    return token if row else None


def checkpoint_route_step(
    conn: sqlite3.Connection, email_id: str, token: str, *, step: str, status: str,
    delivery_intent: RouteDeliveryIntent | None = None,
    delivery_message_ids: list[int] | None = None,
    command_receipt: ReplyCommandReceipt | ProjectApplicationReceipt | None = None,
) -> bool:
    """Record one claimed inbound command boundary without certifying the whole route.

    A started step whose receipt is missing after a crash must be reconciled;
    it is never evidence that re-executing the command is safe. Preserve any
    source checkpoint keys (such as Graph's re-fetch identity).
    """
    if step not in {"fix_candidate", "thread_link", "project_preview", "image_evidence", "inbox_intelligence", "policy_route", "ticket_notice", "operator_card", "reply_agent", "reply_persistence", "reply_card", "project_update_application"}:
        raise ValueError("Unknown inbound route step")
    if status not in {"started", "completed", "incomplete"}:
        raise ValueError("Unknown inbound route step status")
    row = conn.execute("""SELECT checkpoint_json FROM inbound_email_work
        WHERE email_id=? AND route_state='routing' AND lease_token=?
          AND lease_expires_at > datetime('now')""",
        (email_id, token)).fetchone()
    if row is None:
        return False
    checkpoints = json.loads(row[0])
    if not isinstance(checkpoints, dict):
        raise ValueError("Invalid inbound route checkpoint")
    steps = checkpoints.get("route_steps", {})
    if not isinstance(steps, dict):
        raise ValueError("Invalid inbound route steps")
    prior = steps.get(step)
    if (status == "started" and prior is not None) or (
        status != "started" and prior != "started"
    ):
        return False
    if step in {"reply_persistence", "project_update_application"}:
        expected_type = (ReplyCommandReceipt if step == "reply_persistence"
                         else ProjectApplicationReceipt)
        if (status == "started" and command_receipt is not None) or (
            status != "started" and not isinstance(command_receipt, expected_type)
        ):
            raise ValueError(f"{step} completion requires a command receipt")
        if isinstance(command_receipt, ProjectApplicationReceipt):
            # Revalidate copied models and mutable nested identity before recording.
            command_receipt = ProjectApplicationReceipt.model_validate(command_receipt.model_dump())
            if command_receipt.action_command is not None:
                command_receipt.action_command.validate_in(conn)
            if command_receipt.source_ref != email_id or (
                (status == "incomplete") != command_receipt.requires_reconciliation
            ) or command_receipt.status not in {"completed", "failed"} or (
                command_receipt.status == "failed" and not command_receipt.requires_reconciliation
            ):
                raise ValueError("Project application receipt does not match the route outcome")
        if isinstance(command_receipt, ReplyCommandReceipt) and (
            not command_receipt.reply_id or not command_receipt.write_action
        ):
            raise ValueError("Reply persistence completion requires a command receipt")
        commands = checkpoints.get("route_commands", {})
        if not isinstance(commands, dict) or step in commands:
            raise ValueError("Invalid or repeated inbound command receipt")
        if command_receipt is not None:
            checkpoints["route_commands"] = {
                **commands, step: command_receipt.model_dump(),
            }
    elif command_receipt is not None:
        raise ValueError("Command receipt belongs to a claimed command step")
    if delivery_intent is not None or delivery_message_ids is not None:
        if step not in {"ticket_notice", "operator_card", "reply_card"}:
            raise ValueError("Only card steps accept delivery receipts")
    deliveries = checkpoints.get("route_deliveries", {})
    if not isinstance(deliveries, dict):
        raise ValueError("Invalid inbound route deliveries")
    if status == "started" and delivery_intent is not None:
        if delivery_message_ids is not None or step in deliveries:
            return False
        deliveries = {**deliveries, step: {
            **delivery_intent.model_dump(), "state": "sending",
        }}
    elif status == "completed" and step in deliveries:
        if delivery_intent is not None or not isinstance(delivery_message_ids, list) or (
            not delivery_message_ids or any(type(mid) is not int or mid <= 0
                                        for mid in delivery_message_ids)
        ):
            raise ValueError("Card completion requires locally recorded Telegram IDs")
        if deliveries[step].get("state") != "sending":
            return False
        deliveries = {**deliveries, step: {
            **deliveries[step], "state": "locally_recorded",
            "message_ids": delivery_message_ids,
        }}
    elif delivery_intent is not None or delivery_message_ids is not None:
        raise ValueError("Delivery intent belongs to start and IDs to completion")
    checkpoints["route_steps"] = {**steps, step: status}
    if step in deliveries:
        checkpoints["route_deliveries"] = deliveries
    updated = conn.execute("""UPDATE inbound_email_work
        SET checkpoint_json=?, updated_at=datetime('now')
        WHERE email_id=? AND route_state='routing' AND lease_token=?
          AND lease_expires_at > datetime('now') AND checkpoint_json=?""",
        (json.dumps(checkpoints, sort_keys=True), email_id, token, row[0])).rowcount
    return updated == 1


def finish_route(conn: sqlite3.Connection, email_id: str, token: str) -> bool:
    row = conn.execute("""UPDATE inbound_email_work
        SET route_state='completed', lease_token=NULL, lease_expires_at=NULL,
            updated_at=datetime('now')
        WHERE email_id=? AND route_state='routing' AND lease_token=?
        RETURNING email_id""", (email_id, token)).fetchone()
    return row is not None


def require_reconciliation(
    conn: sqlite3.Connection, email_id: str, token: str, *, reason: str,
) -> bool:
    row = conn.execute("""UPDATE inbound_email_work
        SET route_state='needs_reconciliation', lease_token=NULL,
            lease_expires_at=NULL, last_error=?, updated_at=datetime('now')
        WHERE email_id=? AND route_state='routing' AND lease_token=?
        RETURNING email_id""", (reason[:500], email_id, token)).fetchone()
    return row is not None


def expire_routing_leases(conn: sqlite3.Connection, *, source_type: str | None = None) -> int:
    """A crashed route may have committed effects; never automatically retry it."""
    cur = conn.execute("""UPDATE inbound_email_work
        SET route_state='needs_reconciliation', lease_token=NULL,
            lease_expires_at=NULL, last_error='route lease expired; effects unknown',
            updated_at=datetime('now')
        WHERE route_state='routing' AND lease_expires_at <= datetime('now')
          AND (? IS NULL OR source_type=?)""", (source_type, source_type))
    return cur.rowcount


def mark_pending_source_retry(
    conn: sqlite3.Connection, email_id: str, *, source_type: str,
) -> bool:
    """Rotate a transient source failure without claiming or replaying the route."""
    if source_type not in {"imap", "graph"}:
        raise ValueError("Only recoverable source types can retry")
    cursor = conn.execute("""UPDATE inbound_email_work
        SET source_retry_count=source_retry_count+1, updated_at=datetime('now')
        WHERE email_id=? AND source_type=? AND route_state='pending'""",
        (email_id, source_type))
    return cursor.rowcount == 1


def list_pending_imap(conn: sqlite3.Connection, account_key: str, mailbox: str,
                      *, limit: int) -> list[dict]:
    """Read a bounded, durable least-tried batch of never-started routes."""
    rows = conn.execute("""SELECT w.email_id, w.source_type, w.account_key,
               w.mailbox, w.uid_validity, w.source_uid, w.classification_json,
               w.checkpoint_json, p.email_id AS anchor_email_id,
               p.sender_email AS anchor_sender_email, p.subject AS anchor_subject,
               p.result AS anchor_result, p.project_id AS anchor_project_id
        FROM inbound_email_work AS w
        LEFT JOIN processed_emails AS p ON p.email_id=w.email_id
        WHERE w.source_type='imap' AND w.account_key=? AND w.mailbox=?
          AND w.route_state='pending'
        ORDER BY w.source_retry_count, w.captured_at, w.email_id LIMIT ?""",
        (account_key, mailbox, limit))
    return [dict(row) for row in rows]


def list_pending_graph(conn: sqlite3.Connection, account_key: str, mailbox: str,
                       *, limit: int) -> list[dict]:
    """Read only never-started Graph work for this account and folder."""
    rows = conn.execute("""SELECT email_id, source_type, account_key, mailbox,
               uid_validity, source_uid, classification_json, checkpoint_json
        FROM inbound_email_work
        WHERE source_type='graph' AND account_key=? AND mailbox=?
          AND route_state='pending'
        ORDER BY source_retry_count, captured_at, email_id LIMIT ?""",
        (account_key, mailbox, limit))
    return [dict(row) for row in rows]
