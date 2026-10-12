"""Atomic positive attestation for the two ledgers of one combined Telegram send.

Never sends, retries or rebinds. Both original intents and frozen content must
match; an operator must independently verify the original bot and destination.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from src.db.savepoint import savepoint
from src.repositories.activity_log_repo import insert_activity


def has_combined_delivery(conn, email_id: str, *, presentation: bool = False) -> bool:
    if presentation:
        row = conn.execute("SELECT delivery_payload_json FROM inbound_presentation_outbox WHERE email_id=?", (email_id,)).fetchone()
        # Non-null content is only used for combined claims. Corrupt content
        # still goes through the strict pair validator, never ordinary fallback.
        return bool(row and row[0] is not None)
    row = conn.execute("SELECT delivery_mode,delivery_group FROM project_advisory_outbox WHERE email_id=?", (email_id,)).fetchone()
    return bool(row and row[0] == "presentation")


def confirm_combined_receipt(conn, *, email_id: str, message_id: int, actor: str, evidence_ref: str) -> None:
    advisory = conn.execute("SELECT * FROM project_advisory_outbox WHERE email_id=?", (email_id,)).fetchone()
    presentation = conn.execute("SELECT * FROM inbound_presentation_outbox WHERE email_id=?", (email_id,)).fetchone()
    if advisory is None or presentation is None:
        raise ValueError("combined delivery companion unavailable")
    try:
        payload = json.loads(presentation["delivery_payload_json"] or "{}")
    except ValueError as exc:
        raise ValueError("combined content unavailable") from exc
    if not isinstance(payload, dict):
        raise ValueError("combined content invalid")
    if (advisory["delivery_mode"] != "presentation" or not advisory["delivery_group"]
            or payload.get("combined_delivery_group") != advisory["delivery_group"]
            or not payload.get("combined_advisory") or not advisory["advisory_json"]
            or presentation["kind"] != "info_card"
            or payload.get("project_id") != advisory["project_id"]
            or payload.get("email_id") != email_id
            or payload.get("advisory_fingerprint") != hashlib.sha256(advisory["advisory_json"].encode()).hexdigest()
            or payload.get("advisory_snapshot") != advisory["snapshot_json"]
            or payload.get("advisory_semantic_key") != advisory["semantic_key"]
            or any(advisory[k] != presentation[k] for k in ("project_id", "chat_id", "bot_fingerprint"))
            or any(row["state"] != "uncertain" for row in (advisory, presentation))
            or any(row["telegram_message_id"] is not None
                   and str(row["telegram_message_id"]) != str(message_id) for row in (advisory, presentation))):
        raise ValueError("combined delivery scope or companion version changed")
    chat_id = advisory["chat_id"]
    existing = conn.execute("SELECT * FROM telegram_messages WHERE chat_id=? AND telegram_message_id=?",
                            (chat_id, message_id)).fetchone()
    if existing:
        try:
            metadata = json.loads(existing["metadata_json"] or "{}")
        except ValueError as exc:
            raise ValueError("combined receipt metadata invalid") from exc
        if (not isinstance(metadata, dict) or existing["direction"] != "outbound"
                or existing["message_kind"] != "card" or existing["project_id"] != advisory["project_id"]
                or metadata.get("card_profile") != "I" or metadata.get("email_id") != email_id
                or metadata.get("combined_project_advisory") is not True
                or metadata.get("combined_delivery_group") != advisory["delivery_group"]
                or metadata.get("bot_fingerprint") != advisory["bot_fingerprint"]
                or metadata.get("advisory_fingerprint") != payload.get("advisory_fingerprint")
                or metadata.get("analysis_notice") != payload.get("notice")):
            raise ValueError("Telegram message belongs to another delivery")
    source_receipts = conn.execute("""SELECT telegram_message_id FROM telegram_messages
        WHERE chat_id=? AND direction='outbound' AND message_kind='card'
          AND json_extract(CASE WHEN json_valid(metadata_json) THEN metadata_json ELSE '{}' END,
                           '$.email_id')=?""", (chat_id, email_id)).fetchall()
    if source_receipts and existing is None:
        raise ValueError("Telegram receipt conflicts with combined delivery")
    for table, key, own in (
        ("project_advisory_outbox", "email_id", email_id),
        ("inbound_presentation_outbox", "email_id", email_id),
        ("support_email_effects", "email_id", None),
        ("go_live_telegram_outbox", "project_id", None),
        ("email_commitment_card_outbox", "reply_id", None),
        ("staged_update_card_outbox", "operation_id", None),
        ("contact_identity_card_outbox", "suggestion_id", None),
    ):
        claims = conn.execute(f"SELECT {key} FROM {table} WHERE chat_id=? AND CAST(telegram_message_id AS TEXT)=?",
                              (chat_id, str(message_id))).fetchall()
        if any(own is None or row[0] != own for row in claims):
            raise ValueError("Telegram message claimed by another delivery")
    now = datetime.now(timezone.utc).isoformat()
    with savepoint(conn, ensure_transaction=True):
        for table, row, action in (
            ("project_advisory_outbox", advisory, "project_advisory_reconciled"),
            ("inbound_presentation_outbox", presentation, "inbound_presentation_reconciled"),
        ):
            changed = conn.execute(f"""UPDATE {table} SET state='sent',telegram_message_id=?,
                last_error=NULL,lease_token=NULL,lease_until=NULL,updated_at=?
                WHERE email_id=? AND state='uncertain' AND updated_at=? AND attempts=?""",
                (message_id, now, email_id, row["updated_at"], row["attempts"])).rowcount
            if changed != 1:
                raise ValueError("combined receipt version changed")
            insert_activity(conn, actor=actor.strip(), action_type=action, project_id=advisory["project_id"],
                surface="dashboard", object_ref=email_id, touch_project=False,
                summary="Operator attested one combined email/advisory Telegram delivery",
                metadata_json=json.dumps({"decision": "confirm_sent", "combined_delivery_group": advisory["delivery_group"],
                    "evidence_ref": evidence_ref.strip(), "telegram_message_id": message_id,
                    "chat_id": chat_id, "bot_fingerprint": advisory["bot_fingerprint"]}))

