"""
Supervised contact learning for newly routed project-update senders.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading

from src.utils.async_helpers import has_running_loop
import uuid
from datetime import datetime, timezone
from typing import Any

from telegram import Bot

from config.settings import CONFIG
from src.repositories.contact_repo import (
    create_contact,
    create_organisation,
    find_contact_by_email,
    get_organisation_by_domain,
)
from src.wiki.domain_index import get_wiki_domain_index
from src.telegram.cards import build_card, format_card_text
from src.telegram.i18n import t
from src.telegram.markup import card_to_markup
from src.telegram.runtime import send_persisted_message
from src.triage.identity_resolution import resolve_sender


def maybe_enqueue_contact_learning(
    conn: sqlite3.Connection,
    event,
    *,
    sender_name: str | None = None,
    push_card: bool = True,
) -> dict[str, Any] | None:
    """Party-graph identity: global person, project is membership context only.

    - Known contact → link to this project (no identity card).
    - Unknown person → one global pending suggestion; push card at most once
      while pending (not per email, not per project spam).

    ``push_card=False`` leaves the entire transaction with the caller. The
    legacy immediate-push mode commits before external delivery and must not
    be used inside a caller-owned project-update transaction.
    """
    if getattr(event, "classification_type", None) != "project_update":
        return None
    project_id = getattr(event, "project_id", None)
    if not project_id:
        return None

    # Internal staff are not client contacts — never prompt to register them.
    sender_domain = event.sender_email.split("@", 1)[1].lower() if "@" in event.sender_email else ""
    if get_wiki_domain_index().is_internal(sender_domain):
        return None

    identity = resolve_sender(conn, event.sender_email, sender_name=sender_name)
    if identity.contact_id:
        from src.repositories.party_repo import link_contact_to_project, link_org_to_project

        link_contact_to_project(
            conn, identity.contact_id, project_id, source="email_seen",
        )
        if identity.organisation_id:
            role = "vendor" if identity.resolution_method != "exact_email_match" else "other"
            # Prefer org_type from organisations table
            org_row = conn.execute(
                "SELECT org_type FROM organisations WHERE org_id = ?",
                (identity.organisation_id,),
            ).fetchone()
            party_role = (org_row["org_type"] if org_row else None) or "other"
            if party_role not in (
                "client", "vendor", "regulator", "internal", "counterparty", "other",
            ):
                party_role = "other"
            link_org_to_project(
                conn, project_id, identity.organisation_id, party_role,
            )
        if push_card:  # Preserve the legacy direct-call transaction boundary.
            conn.commit()
        return {
            "status": "known_contact",
            "contact_id": identity.contact_id,
            "history_updated": True,
        }

    if not identity.needs_operator_check:
        return None

    from src.repositories.party_repo import guess_org_type_for_domain

    org_type_guess = guess_org_type_for_domain(conn, sender_domain)
    if identity.organisation_name and org_type_guess == "unknown":
        org_type_guess = "vendor"  # known domain org without type → often vendor

    suggestion_id, should_push = _upsert_contact_suggestion(
        conn,
        email_id=getattr(event, "email_id", None),
        sender_email=event.sender_email,
        sender_name=sender_name or _infer_display_name(event.sender_email),
        project_id=project_id,
        organisation_id_guess=identity.organisation_id,
        organisation_name_guess=identity.organisation_name or sender_domain or None,
        resolution_method=identity.resolution_method,
        org_type_guess=org_type_guess,
        commit=push_card,
    )

    push_result = None
    if should_push and push_card:
        push_result = push_identity_check_card(conn, suggestion_id=suggestion_id)

    return {
        "status": "queued",
        "suggestion_id": suggestion_id,
        "card_sent": bool(push_result and push_result.get("sent")),
        "telegram_message_id": push_result.get("telegram_message_id") if push_result else None,
        "should_push": should_push,
    }


def push_identity_check_card(
    conn: sqlite3.Connection | None,
    *,
    suggestion_id: str,
    chat_id: int | None = None,
) -> dict[str, Any]:
    """Send a Telegram identity confirmation card for one pending suggestion."""
    if conn is None:
        from src.db.connection import scoped_read
        with scoped_read() as read_conn:
            suggestion = get_contact_suggestion(read_conn, suggestion_id)
    else:
        suggestion = get_contact_suggestion(conn, suggestion_id)
    if suggestion is None:
        return {"telegram_message_id": None, "sent": False, "error": "suggestion_not_found"}

    token = CONFIG.telegram.bot_token
    fallback_allowed = list(getattr(CONFIG.telegram, "allowed_chat_ids", []) or [])
    resolved_chat_id = chat_id or CONFIG.telegram.chat_id or (fallback_allowed[0] if fallback_allowed else 0)
    if not token or not resolved_chat_id:
        return {"telegram_message_id": None, "sent": False, "error": "telegram_not_configured"}

    if has_running_loop():
        return _run_push_in_thread(
            token=token,
            chat_id=resolved_chat_id,
            suggestion_id=suggestion_id,
        )

    return asyncio.run(
        _push_identity_check_card(
            None if conn is None else conn,
            token=token,
            chat_id=resolved_chat_id,
            suggestion_id=suggestion_id,
        )
    )


def apply_contact_suggestion(
    conn: sqlite3.Connection,
    suggestion_id: str,
    *,
    operator_id: str = "default_operator",
    org_type: str | None = None,
) -> dict[str, str] | None:
    """Apply pending suggestion: global contact + org + project party links.

    ``org_type`` may be forced by operator (client/vendor/…); otherwise uses guess.
    """
    from src.repositories.party_repo import link_contact_to_project, link_org_to_project

    suggestion = get_contact_suggestion(conn, suggestion_id)
    if suggestion is None or suggestion["status"] != "pending":
        return None

    sender_email = (suggestion["sender_email"] or "").strip().lower()
    if not sender_email:
        return None

    domain = sender_email.split("@", 1)[1] if "@" in sender_email else ""
    resolved_org_type = (
        org_type
        or suggestion.get("org_type_guess")
        or "unknown"
    )
    if resolved_org_type not in (
        "client", "vendor", "regulator", "internal", "unknown",
    ):
        resolved_org_type = "unknown"

    organisation_id = suggestion["organisation_id_guess"]
    if not organisation_id and domain:
        existing_org = get_organisation_by_domain(conn, domain)
        if existing_org:
            organisation_id = existing_org["org_id"]
            # Promote type if still unknown
            if (
                existing_org.get("org_type") in (None, "unknown")
                and resolved_org_type != "unknown"
            ):
                conn.execute(
                    "UPDATE organisations SET org_type = ? WHERE org_id = ?",
                    (resolved_org_type, organisation_id),
                )
    if not organisation_id and domain:
        org_name = suggestion["organisation_name_guess"] or domain
        organisation_id = create_organisation(
            conn, org_name, domain, resolved_org_type,
        )
    elif organisation_id and resolved_org_type != "unknown":
        conn.execute(
            """UPDATE organisations SET org_type = ?
               WHERE org_id = ? AND (org_type IS NULL OR org_type = 'unknown')""",
            (resolved_org_type, organisation_id),
        )

    existing_contact = find_contact_by_email(conn, sender_email)
    if existing_contact:
        contact_id = existing_contact["contact_id"]
        conn.execute(
            """UPDATE contacts
               SET created_by = 'operator_confirmed',
                   confidence = 1.0,
                   organisation_id = COALESCE(organisation_id, ?)
               WHERE contact_id = ?""",
            (organisation_id, contact_id),
        )
    else:
        display_name = suggestion["sender_name"] or _infer_display_name(sender_email)
        _wiki_org_type = get_wiki_domain_index().org_type_for(domain)
        _contact_type = "internal" if _wiki_org_type == "internal" else "person"
        contact_id = create_contact(
            conn,
            display_name=display_name,
            email_addresses=sender_email,
            primary_email=sender_email,
            organisation_id=organisation_id,
            contact_type=_contact_type,
        )
        conn.execute(
            """UPDATE contacts
               SET created_by = 'operator_confirmed',
                   confidence = 1.0
               WHERE contact_id = ?""",
            (contact_id,),
        )

    project_id = suggestion.get("project_id")
    if project_id:
        link_contact_to_project(
            conn, contact_id, project_id, source="identity_confirm",
        )
        if organisation_id:
            party_role = (
                resolved_org_type
                if resolved_org_type in (
                    "client", "vendor", "regulator", "internal",
                )
                else "other"
            )
            link_org_to_project(conn, project_id, organisation_id, party_role)

    # Apply all other pending suggestions for same email (global person)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """UPDATE contact_suggestions
           SET status = 'applied',
               applied_contact_id = ?,
               operator_id = ?,
               updated_at = ?
           WHERE lower(sender_email) = ? AND status = 'pending'""",
        (contact_id, operator_id, now, sender_email),
    )
    conn.commit()
    return {
        "suggestion_id": suggestion_id,
        "contact_id": contact_id,
        "organisation_id": organisation_id or "",
        "org_type": resolved_org_type,
    }


def get_contact_suggestion(
    conn: sqlite3.Connection,
    suggestion_id: str,
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM contact_suggestions WHERE suggestion_id = ?",
        (suggestion_id,),
    ).fetchone()
    return dict(row) if row else None


def _upsert_contact_suggestion(
    conn: sqlite3.Connection,
    *,
    email_id: str | None,
    sender_email: str,
    sender_name: str | None,
    project_id: str,
    organisation_id_guess: str | None,
    organisation_name_guess: str | None,
    resolution_method: str,
    org_type_guess: str = "unknown",
    commit: bool = True,
) -> tuple[str, bool]:
    """Upsert global pending suggestion by sender_email.

    Returns (suggestion_id, should_push).
    Push only if no pending card was already pushed (last_pushed_at / telegram_message_id).
    """
    sender_email = sender_email.strip().lower()
    domain = sender_email.split("@", 1)[1] if "@" in sender_email else ""
    now = datetime.now(timezone.utc).isoformat()

    # Global person: any pending row for this email
    row = conn.execute(
        """SELECT suggestion_id, telegram_message_id, last_pushed_at, status, project_id
           FROM contact_suggestions
           WHERE lower(sender_email) = ?
             AND status = 'pending'
           ORDER BY created_at DESC
           LIMIT 1""",
        (sender_email,),
    ).fetchone()
    if row:
        suggestion_id = row["suggestion_id"]
        conn.execute(
            """UPDATE contact_suggestions
               SET sender_name = COALESCE(?, sender_name),
                   project_id = COALESCE(?, project_id),
                   email_id = COALESCE(?, email_id),
                   organisation_id_guess = COALESCE(?, organisation_id_guess),
                   organisation_name_guess = COALESCE(?, organisation_name_guess),
                   organisation_domain = COALESCE(?, organisation_domain),
                   org_type_guess = COALESCE(?, org_type_guess),
                   resolution_method = COALESCE(?, resolution_method),
                   updated_at = ?
               WHERE suggestion_id = ?""",
            (
                sender_name,
                project_id,
                email_id,
                organisation_id_guess,
                organisation_name_guess,
                domain or None,
                org_type_guess,
                resolution_method,
                now,
                suggestion_id,
            ),
        )
        if commit:
            conn.commit()
        already_pushed = bool(row["telegram_message_id"] or row["last_pushed_at"])
        return suggestion_id, not already_pushed

    suggestion_id = f"cs_{uuid.uuid4().hex[:12]}"
    try:
        conn.execute(
            """INSERT INTO contact_suggestions
               (suggestion_id, email_id, sender_email, sender_name, project_id,
                organisation_id_guess, organisation_name_guess, organisation_domain,
                org_type_guess, resolution_method, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                suggestion_id,
                email_id,
                sender_email,
                sender_name,
                project_id,
                organisation_id_guess,
                organisation_name_guess,
                domain or None,
                org_type_guess,
                resolution_method,
                now,
                now,
            ),
        )
    except sqlite3.IntegrityError:
        # UNIQUE(email_id, sender_email, project_id) race — fall back to lookup
        row2 = conn.execute(
            """SELECT suggestion_id, telegram_message_id, last_pushed_at
               FROM contact_suggestions
               WHERE lower(sender_email) = ? AND status = 'pending'
               ORDER BY created_at DESC LIMIT 1""",
            (sender_email,),
        ).fetchone()
        if row2:
            return row2["suggestion_id"], not bool(
                row2["telegram_message_id"] or row2["last_pushed_at"]
            )
        raise
    if commit:
        conn.commit()
    return suggestion_id, True


def _append_cross_project_history(
    conn: sqlite3.Connection,
    contact_id: str,
    project_id: str | None,
) -> bool:
    if not project_id:
        return False
    row = conn.execute(
        "SELECT cross_project_history FROM contacts WHERE contact_id = ?",
        (contact_id,),
    ).fetchone()
    if row is None:
        return False

    history = _parse_history(row["cross_project_history"])
    if project_id in history:
        return False
    history.append(project_id)
    conn.execute(
        "UPDATE contacts SET cross_project_history = ? WHERE contact_id = ?",
        (json.dumps(sorted(history)), contact_id),
    )
    conn.commit()
    return True


def _confirm_existing_contact(
    conn: sqlite3.Connection,
    *,
    contact_id: str,
    sender_email: str,
    sender_name: str | None,
    organisation_id: str | None,
) -> None:
    row = conn.execute(
        """SELECT display_name, email_addresses, primary_email
           FROM contacts WHERE contact_id = ?""",
        (contact_id,),
    ).fetchone()
    if row is None:
        return

    emails = _parse_email_addresses(row["email_addresses"])
    if sender_email not in emails:
        emails.append(sender_email)

    display_name = row["display_name"] or sender_name or _infer_display_name(sender_email)
    primary_email = row["primary_email"] or sender_email
    conn.execute(
        """UPDATE contacts
           SET display_name = ?,
               email_addresses = ?,
               primary_email = ?,
               organisation_id = COALESCE(organisation_id, ?),
               created_by = 'operator_confirmed',
               confidence = 1.0
           WHERE contact_id = ?""",
        (
            display_name,
            ", ".join(sorted(emails)),
            primary_email,
            organisation_id,
            contact_id,
        ),
    )


def _parse_history(raw_value: str | None) -> list[str]:
    if not raw_value:
        return []
    text = raw_value.strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = []
        if isinstance(data, list):
            return [str(item).strip() for item in data if str(item).strip()]
    return [part.strip() for part in text.replace(";", ",").split(",") if part.strip()]


def _parse_email_addresses(raw_value: str | None) -> list[str]:
    return [value.lower() for value in _parse_history(raw_value)]


def _infer_display_name(sender_email: str) -> str:
    local_part = sender_email.split("@", 1)[0]
    words = [part for part in local_part.replace("-", ".").replace("_", ".").split(".") if part]
    if not words:
        return sender_email
    return " ".join(word.capitalize() for word in words)


async def _push_identity_check_card(
    conn: sqlite3.Connection | None,
    *,
    token: str,
    chat_id: int,
    suggestion_id: str,
) -> dict[str, Any]:
    from src.db.connection import scoped_read, scoped_write

    with scoped_read() as rconn:
        suggestion = get_contact_suggestion(rconn, suggestion_id)
    if suggestion is None:
        return {"telegram_message_id": None, "sent": False, "error": "suggestion_not_found"}

    bot = Bot(token=token)
    card = build_card("E", t("contact.identity_title"), _build_identity_body(suggestion))
    for row in card.buttons:
        for button in row:
            action = button.label.upper().replace(" ", "_")
            button.callback_data = f"E:{action}:{suggestion_id}"

    object_links = [
        {
            "object_type": "contact_suggestion",
            "object_ref": suggestion_id,
            "project_id": suggestion.get("project_id"),
        }
    ]
    if suggestion.get("email_id"):
        object_links.append(
            {
                "object_type": "email",
                "object_ref": suggestion["email_id"],
                "project_id": suggestion.get("project_id"),
            }
        )

    # send_persisted_message(conn=None) manages its own brief writes
    message_ids = await send_persisted_message(
        bot,
        None,
        chat_id=chat_id,
        text=format_card_text(card),
        project_id=suggestion.get("project_id"),
        message_kind="card",
        reply_markup=card_to_markup(card),
        object_links=object_links,
        metadata={
            "card_profile": "E",
            "contact_suggestion_id": suggestion_id,
        },
    )
    telegram_message_id = message_ids[-1] if message_ids else None
    now = datetime.now(timezone.utc).isoformat()
    # Always stamp last_pushed_at when we attempted a successful send
    if telegram_message_id is not None:
        with scoped_write() as wconn:
            wconn.execute(
                """UPDATE contact_suggestions
                   SET telegram_message_id = ?, last_pushed_at = ?, updated_at = ?
                   WHERE suggestion_id = ?""",
                (str(telegram_message_id), now, now, suggestion_id),
            )
    return {
        "telegram_message_id": telegram_message_id,
        "sent": telegram_message_id is not None,
        "sent_at": now,
    }


def _build_identity_body(suggestion: dict[str, Any]) -> str:
    """Global person card: org type + domain + context project (Option C)."""
    name = suggestion.get("sender_name") or _infer_display_name(
        suggestion.get("sender_email") or ""
    )
    org = (
        suggestion.get("organisation_name_guess")
        or suggestion.get("organisation_domain")
        or "-"
    )
    org_type = suggestion.get("org_type_guess") or "unknown"
    org_type_zh = {
        "client": "客戶",
        "vendor": "Vendor / 供應商",
        "regulator": "監管",
        "internal": "內部",
        "unknown": "未知",
    }.get(org_type, org_type)
    return t("contact.identity_body").format(
        email=suggestion["sender_email"],
        name=name or "-",
        org=org,
        org_type=org_type_zh,
        project_id=suggestion.get("project_id") or "-",
        method=suggestion.get("resolution_method") or "-",
    )



def _run_push_in_thread(
    *,
    token: str,
    chat_id: int,
    suggestion_id: str,
) -> dict[str, Any]:
    """Push identity check card in a new thread without holding write lock
    during Telegram network I/O."""
    result: dict[str, Any] = {}
    error: list[BaseException] = []

    def _runner() -> None:
        try:
            result["value"] = asyncio.run(
                _push_identity_check_card(
                    None,
                    token=token,
                    chat_id=chat_id,
                    suggestion_id=suggestion_id,
                )
            )
        except BaseException as exc:  # pragma: no cover - defensive bridge
            error.append(exc)

    thread = threading.Thread(target=_runner, daemon=True, name="tg-identity-push")
    thread.start()
    thread.join()
    if error:
        raise error[0]
    return result["value"]
