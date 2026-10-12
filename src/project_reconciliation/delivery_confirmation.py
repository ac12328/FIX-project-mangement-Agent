"""Operator-confirmed delivery authority bound to one unchanged project email.

No global contact role, milestone policy, original email, or PCT authority is
changed. A confirmation only annotates production-session delivery extraction.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.email.body_cleaner import clean_email_body
from src.email.source_authority import normalized_sender
from src.project_reconciliation.contracts import EvidenceRecord, FrozenEvidenceBundle
from src.repositories.activity_log_repo import insert_activity
from src.repositories.evidence_repo import insert_evidence

PREFIX = "delivery-source-confirmation:v1:"
PREDICATE = "provisioning_support_production_session_information_delivered"


class DeliveryConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["production_delivery_source_confirmation"] = "production_delivery_source_confirmation"
    project_id: str
    source_ref: str
    sender_email: str
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    authority: Literal["Demo Provisioning Support"] = "Demo Provisioning Support"
    predicate: Literal["provisioning_support_production_session_information_delivered"] = PREDICATE
    confirmed_by: str
    reason: str = Field(min_length=20, max_length=2000)
    typo_quote: str | None = None
    mistyped_code: str | None = None
    correct_code: str | None = None


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def _can_confirm(conn, actor: str) -> bool:
    row = conn.execute("SELECT role,status FROM dashboard_users WHERE user_id=?", (actor,)).fetchone()
    return bool(row and row["status"] == "active" and row["role"] in {"Admin", "Editor"})


def _authored(record: EvidenceRecord) -> str:
    content = record.content.partition("\n")[2] if record.content.startswith("Subject:") else record.content
    return clean_email_body(content)


def clarified_identity_text(record: EvidenceRecord, confirmation: DeliveryConfirmation) -> str:
    """Use an exact, separately approved typo annotation for identity checks only."""
    if (record.source_ref != confirmation.source_ref
            or normalized_sender(record.sender) != confirmation.sender_email
            or _digest(record.content) != confirmation.content_hash):
        raise ValueError("Confirmation does not match the immutable source")
    text = _authored(record)
    values = (confirmation.typo_quote, confirmation.mistyped_code, confirmation.correct_code)
    if not any(values):
        return text
    if not all(values):
        raise ValueError("A typo clarification requires its exact quote and both codes")
    quote, wrong, right = values
    expected = {confirmation.project_id, re.sub(r"\d{4}$", "", confirmation.project_id)}
    if right not in expected or wrong in expected:
        raise ValueError("Correction must resolve to the selected project")
    if any(not re.fullmatch(r"[A-Z]{2,}(?:\d{4})?", code) for code in (wrong, right)):
        raise ValueError("Invalid project code")
    if "\n" in quote or "\r" in quote or text.count(quote) != 1:
        raise ValueError("Typo quote must identify one authored line")
    # An identity annotation must never rewrite technical identifiers or settings.
    if re.search(r"CompID|SenderID|TargetID|\d{1,3}(?:\.\d{1,3}){3}", quote, re.I):
        raise ValueError("Connection settings cannot be corrected through identity annotations")
    pattern = rf"\b{re.escape(wrong)}\b"
    if len(re.findall(pattern, quote)) != 1:
        raise ValueError("Typo token must occur exactly once in its quote")
    return text.replace(quote, re.sub(pattern, right, quote), 1)


def register_delivery_confirmation(
    conn, *, project_id: str, email_id: str, actor: str, reason: str,
    typo_quote: str | None = None, mistyped_code: str | None = None,
    correct_code: str | None = None,
) -> str:
    """Record an explicit operator statement; caller owns transaction/notification."""
    if not _can_confirm(conn, actor):
        raise ValueError("An active Dashboard Admin or Editor must confirm the source")
    row = conn.execute(
        "SELECT subject,body_snippet,sender_email,received_at FROM processed_emails WHERE email_id=? AND project_id=?",
        (email_id, project_id),
    ).fetchone()
    if row is None:
        raise ValueError("Source email must be linked to the selected project")
    content = f"Subject: {row['subject'] or ''}\n{row['body_snippet'] or ''}".strip()
    sender = normalized_sender(row["sender_email"])
    if not sender:
        raise ValueError("Source sender is invalid")
    confirmation = DeliveryConfirmation(
        project_id=project_id, source_ref=f"processed_emails:{email_id}", sender_email=sender,
        content_hash=_digest(content), confirmed_by=actor, reason=reason,
        typo_quote=typo_quote, mistyped_code=mistyped_code, correct_code=correct_code,
    )
    record = EvidenceRecord(source_ref=confirmation.source_ref, source_table="processed_emails",
        source_id=email_id, project_match="linked", event_at=row["received_at"] or "",
        subject=row["subject"] or "", sender=sender, content=content, content_hash=_digest(content))
    clarified_identity_text(record, confirmation)
    payload = json.dumps(confirmation.model_dump(), ensure_ascii=False, sort_keys=True)
    key = PREFIX + _digest(payload)
    prior = conn.execute("SELECT evidence_id FROM evidence_store WHERE project_id=? AND source_ref=?", (project_id,key)).fetchone()
    if prior:
        return prior[0]
    evidence_id = insert_evidence(conn, project_id, "manual_note",
        "Operator-confirmed production delivery source and project identity (not milestone approval)",
        datetime.now(timezone.utc).isoformat(), content_text=payload, source_ref=key)
    insert_activity(conn, actor=actor, action_type="delivery_source_confirmed", project_id=project_id,
        surface="workspace", object_ref=evidence_id,
        summary="Confirmed authority for one production-session delivery email; no project fields changed.",
        metadata_json=json.dumps({"payload_hash": _digest(payload), "source_ref": confirmation.source_ref,
            "predicate": PREDICATE, "project_id": project_id}), touch_project=False)
    return evidence_id


def load_delivery_confirmations(conn, bundle: FrozenEvidenceBundle) -> dict[str, tuple[str, DeliveryConfirmation]]:
    """Fail closed for edited, unaudited, revoked, conflicting or cross-scope notes."""
    records = {record.source_ref: record for record in bundle.records}
    rows = conn.execute("""SELECT e.evidence_id,e.content_text,e.source_ref FROM evidence_store e
        WHERE e.project_id=? AND e.evidence_type='manual_note' AND e.source_ref LIKE ?""",
        (bundle.project_id, PREFIX + "%")).fetchall()
    found = {}
    conflicted = set()
    for row in rows:
        try:
            payload = row["content_text"] or ""
            confirmation = DeliveryConfirmation.model_validate_json(payload)
            if confirmation.project_id != bundle.project_id or not _can_confirm(conn, confirmation.confirmed_by):
                continue
            if row["source_ref"] != PREFIX + _digest(payload):
                continue
            audit = conn.execute("""SELECT metadata_json FROM activity_log WHERE object_ref=? AND project_id=?
                AND actor=? AND action_type='delivery_source_confirmed'""",
                (row["evidence_id"],bundle.project_id,confirmation.confirmed_by)).fetchone()
            if not audit or json.loads(audit[0]).get("payload_hash") != _digest(payload):
                continue
            if conn.execute("SELECT 1 FROM activity_log WHERE object_ref=? AND action_type='delivery_source_confirmation_revoked'",
                            (row["evidence_id"],)).fetchone():
                continue
            record = records.get(confirmation.source_ref)
            if record is None:
                continue
            clarified_identity_text(record, confirmation)
            if confirmation.source_ref in found:
                conflicted.add(confirmation.source_ref)
            found[confirmation.source_ref] = (row["evidence_id"], confirmation)
        except (ValueError, TypeError, KeyError):
            continue
    return {key:value for key,value in found.items() if key not in conflicted}


def revoke_delivery_confirmation(conn, *, project_id: str, evidence_id: str, actor: str, reason: str) -> None:
    """Append a revocation audit without editing original evidence or emails."""
    if not _can_confirm(conn, actor) or len(reason.strip()) < 10:
        raise ValueError("Revocation requires an active operator and a specific reason")
    row = conn.execute("SELECT source_ref FROM evidence_store WHERE evidence_id=? AND project_id=?",
        (evidence_id, project_id)).fetchone()
    if not row or not str(row[0]).startswith(PREFIX):
        raise ValueError("Confirmation is not linked to this project")
    if not conn.execute("SELECT 1 FROM activity_log WHERE object_ref=? AND action_type='delivery_source_confirmation_revoked'",
                        (evidence_id,)).fetchone():
        insert_activity(conn, actor=actor, action_type="delivery_source_confirmation_revoked",
            project_id=project_id, surface="workspace", object_ref=evidence_id,
            summary=reason, touch_project=False)
