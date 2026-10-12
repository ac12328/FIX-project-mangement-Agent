"""Recipient parsing, canonicalization and Safe Reply All helpers."""

from __future__ import annotations

import json
import re
import sqlite3
from email.utils import getaddresses, parseaddr
from typing import Any, Iterable

_EMAIL_RE = re.compile(r"^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$")
_BANNED_LOCAL_RE = re.compile(
    r"(^|[._+-])(no-?reply|do-?not-?reply|mailer-daemon|postmaster|bounce|bounces)([._+-]|$)",
    re.IGNORECASE,
)


def canonical_email(value: str | None) -> str:
    return (parseaddr(value or "")[1] or value or "").strip().casefold()


def valid_email(value: str | None) -> bool:
    return bool(_EMAIL_RE.fullmatch(canonical_email(value)))


def forbidden_email(value: str | None) -> bool:
    email = canonical_email(value)
    if not valid_email(email):
        return True
    return bool(_BANNED_LOCAL_RE.search(email.split("@", 1)[0]))


def parse_recipient_list(
    value: str | Iterable[str] | None,
    *,
    source: str = "manual",
    contact_lookup: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    if value is None:
        return []
    values = [value] if isinstance(value, str) else list(value)
    raw = [item.replace(";", ",") for item in values if item]
    recipients: list[dict[str, Any]] = []
    seen: set[str] = set()
    for display_name, address in getaddresses(raw):
        canonical = canonical_email(address)
        if not canonical or canonical in seen:
            continue
        seen.add(canonical)
        recipients.append(
            {
                "email": address.strip() or canonical,
                "display_name": display_name.strip() or None,
                "contact_id": (contact_lookup or {}).get(canonical),
                "source": source,
            }
        )
    return recipients


def normalize_recipient(value: dict[str, Any], *, default_source: str = "manual") -> dict[str, Any]:
    email = str(value.get("email") or "").strip()
    return {
        "email": email,
        "display_name": (str(value.get("display_name") or "").strip() or None),
        "contact_id": (str(value.get("contact_id") or "").strip() or None),
        "source": str(value.get("source") or default_source),
    }


def serialize_recipient_header(
    structured: list[dict[str, Any]] | None,
    fallback: str | Iterable[str] | None = None,
    *,
    source: str = "thread_participant",
) -> str:
    """Serialize a structured recipient header with a legacy string fallback."""
    rows = [
        normalize_recipient(item, default_source=source)
        for item in (structured or [])
        if isinstance(item, dict)
    ]
    if not rows:
        rows = parse_recipient_list(fallback, source=source)
    return json.dumps(rows, ensure_ascii=False)


def normalize_envelope(value: dict[str, Any] | str | None) -> dict[str, list[dict[str, Any]]]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            value = {}
    value = value or {}
    result: dict[str, list[dict[str, Any]]] = {"to": [], "cc": [], "bcc": []}
    globally_seen: set[str] = set()
    for bucket in ("to", "cc", "bcc"):
        rows = value.get(bucket) or []
        if isinstance(rows, str):
            rows = parse_recipient_list(rows)
        for raw in rows:
            recipient = normalize_recipient(raw) if isinstance(raw, dict) else normalize_recipient({"email": raw})
            canonical = canonical_email(recipient["email"])
            if not canonical or canonical in globally_seen:
                continue
            globally_seen.add(canonical)
            result[bucket].append(recipient)
    return result


def envelope_to_legacy(envelope: dict[str, list[dict[str, Any]]]) -> tuple[str | None, str | None, str | None]:
    normalized = normalize_envelope(envelope)
    render = lambda bucket: ", ".join(_format_recipient(item) for item in normalized[bucket]) or None
    return render("to"), render("cc"), render("bcc")


def _format_recipient(recipient: dict[str, Any]) -> str:
    display_name = recipient.get("display_name")
    email = recipient.get("email") or ""
    return f"{display_name} <{email}>" if display_name else email


def recipients_as_addresses(envelope: dict[str, list[dict[str, Any]]], bucket: str) -> list[str]:
    return [canonical_email(item.get("email")) for item in normalize_envelope(envelope)[bucket]]


def configured_self_identities(email_config: dict[str, Any]) -> set[str]:
    identities: set[str] = set()
    for item in email_config.get("sender_identities") or []:
        email = canonical_email(item.get("email_address"))
        if email:
            identities.add(email)
    for key in ("from_email", "smtp_username", "user_email"):
        email = canonical_email(email_config.get(key))
        if email:
            identities.add(email)
    return identities


def safe_reply_all(
    conn: sqlite3.Connection,
    email_id: str,
    *,
    self_identities: set[str],
) -> dict[str, list[dict[str, Any]]]:
    """Compute a conservative reply-all envelope from stored message headers."""
    row = conn.execute(
        """SELECT email_id, sender_email, direction, to_recipients_json,
                  cc_recipients_json, cc
           FROM processed_emails WHERE email_id=?""",
        (email_id,),
    ).fetchone()
    if not row:
        raise ValueError("Reply email not found")

    to_rows = _stored_recipients(row["to_recipients_json"])
    cc_rows = _stored_recipients(row["cc_recipients_json"])
    if not cc_rows:
        cc_rows = parse_recipient_list(row["cc"], source="thread_participant")
    sender = parse_recipient_list(row["sender_email"], source="thread_participant")
    direction = (row["direction"] or "inbound").casefold()

    if direction == "outbound":
        result = {"to": to_rows, "cc": cc_rows, "bcc": []}
    else:
        result = {
            "to": sender,
            "cc": [*to_rows, *cc_rows],
            "bcc": [],
        }

    filtered: dict[str, list[dict[str, Any]]] = {"to": [], "cc": [], "bcc": []}
    seen: set[str] = set()
    for bucket in ("to", "cc"):
        for recipient in result[bucket]:
            canonical = canonical_email(recipient.get("email"))
            if (
                not canonical
                or canonical in seen
                or canonical in self_identities
                or forbidden_email(canonical)
            ):
                continue
            seen.add(canonical)
            filtered[bucket].append(recipient)
    return filtered


def _stored_recipients(value: str | None) -> list[dict[str, Any]]:
    try:
        rows = json.loads(value or "[]")
    except json.JSONDecodeError:
        rows = []
    if not isinstance(rows, list):
        return []
    return [normalize_recipient(item, default_source="thread_participant") for item in rows if isinstance(item, dict)]
