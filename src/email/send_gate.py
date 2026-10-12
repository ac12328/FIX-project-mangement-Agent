"""Single mandatory preflight gate for every draft delivery path."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field as dc_field
from typing import Any

from config.settings import CONFIG
from src.email.attachments import ALLOWED_EXTENSIONS, MAX_FILE_SIZE, MAX_TOTAL_SIZE
from src.email.draft_session import validate_draft_content
from src.email.draft_safety import validate_draft_safety
from src.email.draft_workspace import (
    build_workspace_snapshot,
    compute_send_affecting_hash,
    sender_identities,
)
from src.email.recipients import (
    canonical_email,
    configured_self_identities,
    forbidden_email,
    normalize_envelope,
    safe_reply_all,
    valid_email,
)
from src.email.sending import email_config_dict


@dataclass(frozen=True)
class PreflightIssue:
    code: str
    message: str
    field: str = "general"
    severity: str = "blocker"
    metadata: dict[str, Any] = dc_field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "field": self.field,
            "severity": self.severity,
            "metadata": self.metadata,
        }


@dataclass
class PreflightResult:
    snapshot_hash: str
    revision: int
    blockers: list[PreflightIssue] = dc_field(default_factory=list)
    warnings: list[PreflightIssue] = dc_field(default_factory=list)
    preview: dict[str, Any] = dc_field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.blockers

    @property
    def warning_codes(self) -> set[str]:
        return {issue.code for issue in self.warnings}

    def require_warning_confirmations(self, confirmed: list[str] | set[str] | None) -> None:
        missing = self.warning_codes - set(confirmed or [])
        if missing:
            raise ValueError(f"Unconfirmed preflight warnings: {', '.join(sorted(missing))}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "snapshot_hash": self.snapshot_hash,
            "revision": self.revision,
            "blockers": [issue.to_dict() for issue in self.blockers],
            "warnings": [issue.to_dict() for issue in self.warnings],
            "preview": self.preview,
        }


class SendGateBlockedError(RuntimeError):
    def __init__(self, result: PreflightResult):
        super().__init__("Draft is blocked by mandatory preflight")
        self.result = result


class SendGateService:
    """Validate a server-side snapshot before queueing or dispatching delivery."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        email_config: dict[str, Any] | None = None,
    ):
        self.conn = conn
        self.email_config = (
            email_config if email_config is not None else email_config_dict(CONFIG)
        )

    def preflight(
        self,
        snapshot_or_id: dict[str, Any] | str,
        *,
        expected_revision: int | None = None,
        expected_hash: str | None = None,
        action_intent: dict[str, Any] | None = None,
    ) -> PreflightResult:
        snapshot = (
            build_workspace_snapshot(self.conn, snapshot_or_id)
            if isinstance(snapshot_or_id, str)
            else snapshot_or_id
        )
        computed_hash = compute_send_affecting_hash(snapshot)
        revision = int((snapshot.get("revision") or {}).get("number") or 0)
        result = PreflightResult(
            snapshot_hash=computed_hash,
            revision=revision,
            preview=self.final_preview(snapshot),
        )

        stored_hash = (snapshot.get("revision") or {}).get("send_affecting_hash")
        if stored_hash and stored_hash != computed_hash:
            self._block(result, "unsaved_snapshot", "Draft snapshot changed after the last successful autosave.", "revision")
        if expected_revision is not None and revision != expected_revision:
            self._block(result, "revision_mismatch", "Draft revision no longer matches the requested version.", "revision")
        if expected_hash is not None and computed_hash != expected_hash:
            self._block(result, "hash_mismatch", "Draft content no longer matches the requested version.", "revision")

        self._validate_sender(snapshot, result)
        self._validate_recipients(snapshot, result)
        self._validate_delivery_context(snapshot, result)
        self._validate_content_and_attachments(snapshot, result)
        self._validate_review(snapshot, computed_hash, result)
        self._validate_action_intent(action_intent, revision, computed_hash, result)
        return result

    def enforce(
        self,
        snapshot_or_id: dict[str, Any] | str,
        *,
        confirmed_warnings: list[str] | set[str] | None = None,
        expected_revision: int | None = None,
        expected_hash: str | None = None,
        action_intent: dict[str, Any] | None = None,
    ) -> PreflightResult:
        result = self.preflight(
            snapshot_or_id,
            expected_revision=expected_revision,
            expected_hash=expected_hash,
            action_intent=action_intent,
        )
        if not result.ok:
            raise SendGateBlockedError(result)
        result.require_warning_confirmations(confirmed_warnings)
        return result

    def final_preview(self, snapshot_or_id: dict[str, Any] | str) -> dict[str, Any]:
        snapshot = (
            build_workspace_snapshot(self.conn, snapshot_or_id)
            if isinstance(snapshot_or_id, str)
            else snapshot_or_id
        )
        envelope = normalize_envelope(snapshot.get("envelope"))
        identity_map = {item["identity_id"]: item for item in sender_identities(self.email_config)}
        identity = identity_map.get((snapshot.get("envelope") or {}).get("sender_identity_id"))
        context = snapshot.get("delivery_context") or {}
        thread = None
        if context.get("delivery_mode") == "reply" and context.get("reply_to_email_id"):
            row = self.conn.execute(
                """SELECT email_id, subject, sender_email, direction, thread_id,
                          in_reply_to, references_chain, received_at, processed_at
                   FROM processed_emails WHERE email_id=?""",
                (context["reply_to_email_id"],),
            ).fetchone()
            thread = dict(row) if row else {"email_id": context["reply_to_email_id"], "missing": True}
        return {
            "from": identity,
            "to": envelope["to"],
            "cc": envelope["cc"],
            "bcc": envelope["bcc"],
            "subject": (snapshot.get("content") or {}).get("subject") or "",
            "body_html": (snapshot.get("content") or {}).get("body_html") or "",
            "body_text": (snapshot.get("content") or {}).get("body_text") or "",
            "attachments": snapshot.get("attachments") or [],
            "delivery_mode": context.get("delivery_mode") or "unresolved",
            "thread": thread,
        }

    def _validate_sender(self, snapshot: dict[str, Any], result: PreflightResult) -> None:
        identity_id = (snapshot.get("envelope") or {}).get("sender_identity_id")
        identities = {item["identity_id"]: item for item in sender_identities(self.email_config)}
        if not identity_id or identity_id not in identities:
            self._block(result, "sender_not_allowlisted", "Select an allowlisted sender identity.", "envelope.sender_identity_id")

    def _validate_recipients(self, snapshot: dict[str, Any], result: PreflightResult) -> None:
        envelope = normalize_envelope(snapshot.get("envelope"))
        if not envelope["to"]:
            self._block(result, "missing_to", "At least one valid To recipient is required.", "envelope.to")
        self_ids = configured_self_identities(self.email_config)
        seen: set[str] = set()
        for bucket in ("to", "cc", "bcc"):
            for recipient in envelope[bucket]:
                email = canonical_email(recipient.get("email"))
                field_name = f"envelope.{bucket}"
                if not valid_email(email):
                    self._block(result, "invalid_recipient", f"Invalid recipient address: {recipient.get('email') or ''}", field_name)
                    continue
                if email in seen:
                    self._block(result, "duplicate_recipient", f"Recipient appears more than once: {email}", field_name)
                seen.add(email)
                if email in self_ids:
                    self._block(result, "self_recipient", f"Configured sender identity cannot be a recipient: {email}", field_name)
                if forbidden_email(email):
                    self._block(result, "forbidden_recipient", f"Blocked no-reply, bounce, or system address: {email}", field_name)
                if recipient.get("source") == "manual" and not recipient.get("contact_id"):
                    self._warn(result, "manual_external_recipient", f"{email} is not linked to a project contact.", field_name)
        if envelope["bcc"]:
            self._warn(result, "bcc_present", "BCC recipients will be hidden from other recipients.", "envelope.bcc")

    def _validate_delivery_context(self, snapshot: dict[str, Any], result: PreflightResult) -> None:
        context = snapshot.get("delivery_context") or {}
        mode = context.get("delivery_mode") or "unresolved"
        if mode == "unresolved":
            self._block(result, "delivery_context_unresolved", "Confirm New email or select a customer reply thread.", "delivery_context")
            return
        if mode == "new":
            if context.get("reply_to_email_id"):
                self._block(result, "new_email_has_reply_headers", "New email mode cannot carry reply metadata.", "delivery_context")
            return
        if mode != "reply":
            self._block(result, "invalid_delivery_mode", "Delivery mode is invalid.", "delivery_context.delivery_mode")
            return
        reply_id = context.get("reply_to_email_id")
        if not reply_id:
            self._block(result, "missing_reply_anchor", "Reply mode requires a confirmed reply anchor.", "delivery_context.reply_to_email_id")
            return
        row = self.conn.execute(
            "SELECT email_id, project_id, subject FROM processed_emails WHERE email_id=?",
            (reply_id,),
        ).fetchone()
        if not row:
            self._block(result, "reply_anchor_missing", "The selected reply email no longer exists.", "delivery_context.reply_to_email_id")
            return
        if snapshot.get("project_id") and row["project_id"] != snapshot.get("project_id"):
            self._block(result, "reply_project_mismatch", "The selected reply thread belongs to another project.", "delivery_context.reply_to_email_id")
            return
        try:
            expected = safe_reply_all(
                self.conn,
                reply_id,
                self_identities=configured_self_identities(self.email_config),
            )
        except ValueError:
            self._block(result, "reply_anchor_missing", "The selected reply email cannot be resolved.", "delivery_context.reply_to_email_id")
            return
        actual = normalize_envelope(snapshot.get("envelope"))
        expected_addresses = {
            canonical_email(item.get("email"))
            for bucket in ("to", "cc")
            for item in expected[bucket]
        }
        actual_addresses = {
            canonical_email(item.get("email"))
            for bucket in ("to", "cc")
            for item in actual[bucket]
        }
        if expected_addresses and not expected_addresses.intersection(actual_addresses):
            self._block(result, "thread_recipient_mismatch", "Recipients are unrelated to the confirmed reply thread.", "envelope")
        else:
            added = sorted(actual_addresses - expected_addresses)
            removed = sorted(expected_addresses - actual_addresses)
            if added:
                self._warn(result, "reply_participant_added", "Recipients were added beyond Safe Reply All.", "envelope", {"recipients": added})
            if removed:
                self._warn(result, "reply_participant_removed", "Some Safe Reply All participants were removed.", "envelope", {"recipients": removed})
        subject = ((snapshot.get("content") or {}).get("subject") or "").strip()
        if subject and not subject.casefold().startswith("re:"):
            self._warn(result, "reply_subject_changed", "Reply subject does not begin with Re:.", "content.subject")

    def _validate_content_and_attachments(self, snapshot: dict[str, Any], result: PreflightResult) -> None:
        content = snapshot.get("content") or {}
        valid, reason = validate_draft_content(
            content.get("subject") or "",
            content.get("body_text") or "",
            for_send=True,
        )
        if not valid:
            self._block(result, "invalid_content", reason or "Draft content is invalid.", "content")
        attachments = snapshot.get("attachments") or []
        total = 0
        for attachment in attachments:
            size = int(attachment.get("size") or 0)
            total += size
            if size > MAX_FILE_SIZE:
                self._block(result, "attachment_too_large", f"Attachment exceeds the per-file limit: {attachment.get('filename')}", "attachments")
            from pathlib import Path
            if Path(str(attachment.get("filename") or "")).suffix.casefold() not in ALLOWED_EXTENSIONS:
                self._block(result, "attachment_type_blocked", f"Attachment type is not allowed: {attachment.get('filename')}", "attachments")
            if attachment.get("sha256") == "missing":
                self._block(result, "attachment_missing", f"Attachment is missing: {attachment.get('filename')}", "attachments")
        if total > MAX_TOTAL_SIZE:
            self._block(result, "attachments_total_too_large", "Total attachment size exceeds the configured limit.", "attachments")

        legacy_draft = {
            "draft_id": snapshot.get("draft_id"),
            "project_id": snapshot.get("project_id"),
            "subject": content.get("subject"),
            "current_draft_text": content.get("body_text"),
            "to_email": ((snapshot.get("envelope") or {}).get("to") or [{}])[0].get("email"),
            "context_packet_ref": (
                (snapshot.get("delivery_context") or {}).get("reply_to_email_id")
                if (snapshot.get("delivery_context") or {}).get("delivery_mode") == "reply"
                else None
            ),
            "task_type": snapshot.get("task_type"),
        }
        safety = validate_draft_safety(self.conn, legacy_draft, data_dir=CONFIG.data_dir, phase="send")
        for issue in safety.issues:
            if issue.code == "recipient_mismatch":
                self._warn(result, issue.code, issue.message, issue.field)
            else:
                self._block(result, issue.code, issue.message, issue.field)

    def _validate_review(self, snapshot: dict[str, Any], computed_hash: str, result: PreflightResult) -> None:
        from src.governance.four_eyes import team_four_eyes_enabled

        review = snapshot.get("collaboration") or {}
        state = review.get("review_state") or "not_requested"
        if team_four_eyes_enabled() and state in {"not_requested", "cancelled"}:
            self._block(
                result,
                "review_required",
                "Another Editor or Admin must approve this client email before send.",
                "collaboration.review_state",
            )
        if state == "requested":
            self._block(result, "review_pending", "Draft is locked while review is pending.", "collaboration.review_state")
        elif state == "changes_requested":
            self._block(result, "review_changes_requested", "Requested review changes must be resolved and resubmitted.", "collaboration.review_state")
        elif state == "stale":
            self._block(result, "approval_stale", "Approval became stale after send-affecting changes.", "collaboration.review_state")
        elif state == "approved" and review.get("approval_hash") != computed_hash:
            self._block(result, "approval_hash_mismatch", "Approval does not match the current draft snapshot.", "collaboration.approval_hash")

    def _validate_action_intent(
        self,
        action_intent: dict[str, Any] | None,
        revision: int,
        computed_hash: str,
        result: PreflightResult,
    ) -> None:
        if not action_intent:
            return
        target_version = str(action_intent.get("target_version") or "")
        if target_version and target_version not in {str(revision), f"workspace:{revision}"}:
            self._block(result, "action_intent_version_mismatch", "Approved action targets another draft revision.", "action_intent")
        payload_hash = action_intent.get("payload_hash")
        payload = action_intent.get("payload") or {}
        bound_hash = payload.get("send_affecting_hash") if isinstance(payload, dict) else None
        if bound_hash and bound_hash != computed_hash:
            self._block(result, "action_intent_hash_mismatch", "Approved action no longer matches the draft snapshot.", "action_intent")
        if payload_hash and action_intent.get("snapshot_hash") and action_intent["snapshot_hash"] != computed_hash:
            self._block(result, "action_intent_hash_mismatch", "Approved action payload is stale.", "action_intent")

    @staticmethod
    def _block(result: PreflightResult, code: str, message: str, field_name: str, metadata: dict[str, Any] | None = None) -> None:
        if code not in {issue.code for issue in result.blockers}:
            result.blockers.append(PreflightIssue(code, message, field_name, "blocker", metadata or {}))

    @staticmethod
    def _warn(result: PreflightResult, code: str, message: str, field_name: str, metadata: dict[str, Any] | None = None) -> None:
        if code not in {issue.code for issue in result.warnings}:
            result.warnings.append(PreflightIssue(code, message, field_name, "warning", metadata or {}))
