"""
Email processor — bridge between InboxFetcher and the triage/routing pipeline.

Two public functions:
  process_fetched_email()  — runs triage, records in DB, returns ClassifiedEvent
  process_sent_copy()      — records outbound email as processed_emails anchor for threading
"""

import hashlib
import logging
from collections.abc import Callable
import sqlite3
from datetime import datetime, timezone

from config.defaults import EMAIL_BODY_STORE_CHARS
from src.email.inbox_fetcher import FetchedEmail
from src.email.recipients import serialize_recipient_header
from src.events.bus import Event, event_bus
from src.triage.email_triage import ClassifiedEvent

logger = logging.getLogger(__name__)


def classify_fetched_email(
    conn: sqlite3.Connection | None,
    fetched: FetchedEmail,
    *,
    llm_classify_fn=None,
) -> ClassifiedEvent:
    """Read-only triage: classify an email without writing to DB.

    Safe to call with a read connection — no INSERT/UPDATE executed.
    LLM classification (stage 3) may run here, so this can be slow.

    When *conn* is ``None``, opens a short ``scoped_read()`` internally.
    Use with ``asyncio.to_thread()`` to keep LLM work off the event loop.
    """
    if conn is None:
        from src.db.connection import scoped_read

        with scoped_read() as rconn:
            return _classify_fetched_email_with_conn(
                rconn, fetched, llm_classify_fn=llm_classify_fn
            )
    return _classify_fetched_email_with_conn(
        conn, fetched, llm_classify_fn=llm_classify_fn
    )


def _classify_fetched_email_with_conn(
    conn: sqlite3.Connection,
    fetched: FetchedEmail,
    *,
    llm_classify_fn=None,
) -> ClassifiedEvent:
    from src.triage.email_triage import triage_email

    return triage_email(
        conn,
        email_id=fetched.email_id,
        sender_email=fetched.sender_email,
        subject=fetched.subject,
        body=fetched.body,
        in_reply_to=fetched.in_reply_to,
        llm_classify_fn=llm_classify_fn,
    )


def persist_classified_email(
    conn: sqlite3.Connection,
    fetched: FetchedEmail,
    event: ClassifiedEvent,
) -> tuple[ClassifiedEvent, bool]:
    """Write-only: persist an already-classified email to processed_emails.

    Must be called with a write connection.  Fast — no LLM calls.
    Caller must use ``scoped_write()`` or call ``commit()`` explicitly.

    Returns:
        (event, is_new) where *is_new* is True only when a new row was inserted.
    """
    now = datetime.now(timezone.utc).isoformat()
    thread_fp = _derive_thread_fp(fetched)
    # Subject verified project tags link to known projects when triage left project_id empty
    from src.email.project_tag_link import resolve_project_id_from_subject

    linked_project_id = resolve_project_id_from_subject(
        conn,
        fetched.subject,
        existing_project_id=event.project_id,
    )
    if linked_project_id and linked_project_id != event.project_id:
        try:
            event.project_id = linked_project_id
        except Exception:
            pass
    cur = conn.execute(
        """INSERT OR IGNORE INTO processed_emails
           (email_id, project_id, processed_at, received_at, result, thread_fp, sender_email, subject, direction,
            thread_id, in_reply_to, references_chain, cc, body_snippet,
            to_recipients_json, cc_recipients_json, bcc_recipients_json, sent_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            fetched.email_id,
            linked_project_id or event.project_id,
            now,
            fetched.received_at or now,
            event.classification_type,
            thread_fp,
            fetched.sender_email,
            fetched.subject,
            "inbound",
            thread_fp,
            fetched.in_reply_to,
            fetched.references,
            fetched.cc,
            fetched.body[:EMAIL_BODY_STORE_CHARS] if fetched.body else None,
            serialize_recipient_header(fetched.to_recipients, fetched.to_emails),
            serialize_recipient_header(fetched.cc_recipients, fetched.cc),
            serialize_recipient_header(fetched.bcc_recipients, fetched.bcc),
            fetched.sent_at if isinstance(getattr(fetched, "sent_at", None), str) else None,
        ),
    )
    is_new = cur.rowcount > 0

    if is_new:
        logger.info(
            "Processed email %s: type=%s project=%s confidence=%.2f",
            fetched.email_id,
            event.classification_type,
            event.project_id,
            event.confidence,
        )
    else:
        logger.debug("Email %s already in processed_emails — skipping re-notification", fetched.email_id)

    if is_new:
        from src.email.project_support_evidence import capture_project_support_evidence

        capture_project_support_evidence(conn, fetched, event)

    # NOTE: image attachment processing is intentionally NOT done here.
    # describe_email_images() makes a vision-LLM call that can take many
    # seconds; running it inside persist_classified_email_batch (which is
    # wrapped in scoped_write) would hold the process-wide write lock for
    # the full LLM latency, starving all other writers. The scheduler calls
    # process_image_attachments separately after releasing the write lock.
    return event, is_new


def persist_classified_email_batch(
    conn: sqlite3.Connection,
    items: list[tuple[FetchedEmail, ClassifiedEvent]],
) -> list[tuple[FetchedEmail, ClassifiedEvent, bool]]:
    """Write-only batch persist for multiple classified emails.

    One transaction for all rows — caller must use ``scoped_write()``.
    No inner ``commit()``; WriteSerializer commits on context exit.

    Returns:
        List of ``(fetched, event, is_new)`` in the same order as *items*.
    """
    results: list[tuple[FetchedEmail, ClassifiedEvent, bool]] = []
    for fetched, event in items:
        persisted_event, is_new = persist_classified_email(conn, fetched, event)
        results.append((fetched, persisted_event, is_new))
    return results


def process_fetched_email(
    conn: sqlite3.Connection,
    fetched: FetchedEmail,
    *,
    llm_classify_fn=None,
) -> ClassifiedEvent:
    """
    Run the 3-stage triage pipeline on one FetchedEmail and record it.

    Stages (inside triage_email):
      1. Deterministic: thread_id / in_reply_to / contact look-up
      2. Heuristic:     domain / FIX keyword counting
      3. LLM-assisted:  if llm_classify_fn provided and confidence < threshold

    After classification, inserts a row into processed_emails so future
    replies to this email can be matched back via find_thread_project().

    Note: prefer calling classify_fetched_email() + persist_classified_email()
    separately to avoid holding a write lock during LLM calls.
    When not using ``scoped_write()``, the caller must ``commit()`` after this call.

    Args:
        conn: SQLite connection.
        fetched: A FetchedEmail from ImapInboxFetcher (or GraphInboxFetcher).
        llm_classify_fn: Optional callable for Stage 3 LLM classification.

    Returns:
        ClassifiedEvent with classification_type, project_id, confidence, etc.
    """
    event = classify_fetched_email(conn, fetched, llm_classify_fn=llm_classify_fn)
    event, is_new = persist_classified_email(conn, fetched, event)
    conn.commit()
    if is_new:
        publish_project_email_processed(fetched, event)
    return event


def process_sent_copy(
    conn: sqlite3.Connection,
    fetched: FetchedEmail,
) -> str | None:
    """
    Record a user-sent email (from Sent Items) as a processed_emails anchor.

    This enables find_thread_project() to match future incoming replies
    back to the correct project via the In-Reply-To header.

    Does NOT run triage — sent emails are not classified.
    Caller must use ``scoped_write()`` or call ``commit()`` explicitly.

    Args:
        conn: SQLite connection.
        fetched: A FetchedEmail with folder="sent" from fetch_sent().
    """
    now = datetime.now(timezone.utc).isoformat()
    thread_fp = _derive_thread_fp(fetched)
    # Try to find project from thread if available
    project_id = None
    if fetched.in_reply_to or fetched.references:
        from src.email.threading import find_thread_project
        project_id = find_thread_project(conn, in_reply_to=fetched.in_reply_to, references=fetched.references)
    from src.email.project_tag_link import resolve_project_id_from_subject
    project_id = resolve_project_id_from_subject(
        conn, fetched.subject, existing_project_id=project_id,
    )
    conn.execute(
        """INSERT OR IGNORE INTO processed_emails
           (email_id, project_id, processed_at, received_at, result, thread_fp, sender_email, subject, direction,
            thread_id, in_reply_to, references_chain, cc, body_snippet,
            to_recipients_json, cc_recipients_json, bcc_recipients_json, sent_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            fetched.email_id,
            project_id,
            now,
            fetched.received_at or now,
            "sent_outbound",
            thread_fp,
            fetched.sender_email,
            fetched.subject,
            "outbound",
            thread_fp,
            fetched.in_reply_to,
            fetched.references,
            fetched.cc,
            fetched.body[:EMAIL_BODY_STORE_CHARS] if fetched.body else None,
            serialize_recipient_header(fetched.to_recipients, fetched.to_emails),
            serialize_recipient_header(fetched.cc_recipients, fetched.cc),
            serialize_recipient_header(fetched.bcc_recipients, fetched.bcc),
            fetched.sent_at if isinstance(getattr(fetched, "sent_at", None), str) else None,
        ),
    )
    logger.debug("Recorded sent copy: email_id=%s subject=%s", fetched.email_id, fetched.subject)
    return project_id


def publish_project_email_processed(
    fetched: FetchedEmail,
    event: ClassifiedEvent | None = None,
    *,
    project_id: str | None = None,
    classification: str | None = None,
    direction: str | None = None,
) -> None:
    """Publish a project-scoped email event after the surrounding transaction commits."""
    resolved_project_id = project_id or (event.project_id if event else None)
    if not resolved_project_id:
        return
    event_bus.publish(
        Event(
            event_type="project_email_processed",
            project_id=resolved_project_id,
            detail={
                "email_id": fetched.email_id,
                "classification": classification or (event.classification_type if event else None),
                "direction": direction or ("outbound" if fetched.folder == "sent" else "inbound"),
                "evidence_at": fetched.received_at,
            },
            surface="system",
        )
    )


def _derive_thread_fp(fetched: FetchedEmail) -> str | None:
    """Derive a thread fingerprint from email headers.

    Uses the root Message-ID of the thread: first entry in References,
    or In-Reply-To, or the email's own Message-ID as fallback.
    """
    if fetched.references:
        first_ref = fetched.references.strip().split()[0].strip("<>")
        if first_ref:
            return first_ref
    if fetched.in_reply_to:
        return fetched.in_reply_to.strip("<>")
    return fetched.email_id


def process_image_attachments(
    conn: sqlite3.Connection | None,
    fetched: FetchedEmail,
    project_id: str,
    *, on_result: Callable[[dict], None] | None = None,
) -> list[dict]:
    """Save image attachments to disk, describe via LLM, optionally store as evidence.

    When *conn* is ``None`` the evidence insertion is skipped so the caller
    can run the LLM work outside a write lock and insert evidence in a
    separate short ``scoped_write()``.

    Returns list of description dicts (may be empty on failure).
    """
    from src.email.image_store import save_email_images
    from src.email.image_describer import describe_email_images
    from src.repositories.evidence_repo import insert_evidence

    image_attachments = [a for a in fetched.attachments if a.content_type.startswith("image/")]
    if not image_attachments:
        return []

    if any(not image.data or image.size_bytes != len(image.data) for image in image_attachments):
        raise ValueError("Selected images have missing payload bytes")

    paths = save_email_images(fetched.email_id, image_attachments)
    if not paths:
        return []

    if len(paths) != len(image_attachments):
        raise ValueError("Saved image count differs from selected payload count")
    expected = {}
    for path, item in zip(paths, image_attachments):
        index = next(i for i, attachment in enumerate(fetched.attachments) if attachment is item)
        expected[path] = (item.mime_part_id or f"selected-{index}", hashlib.sha256(item.data).hexdigest())

    def bind_result(result):
        identity = expected.get(result.get("path"))
        if identity is None or result.get("payload_sha256") != identity[1]:
            raise ValueError("Image output is not bound to selected payload bytes")
        if not isinstance(result.get("description"), str) or not result["description"].strip():
            raise ValueError("Image output description is empty")
        bound = {**result, "part_id": identity[0]}
        if on_result is not None:
            on_result(bound)
        return bound

    descriptions = describe_email_images(paths, on_result=bind_result if on_result else None)
    bound_descriptions = []
    for result in descriptions:
        identity = expected.get(result.get("path"))
        if (identity is None or result.get("payload_sha256") != identity[1]
                or not isinstance(result.get("description"), str) or not result["description"].strip()):
            raise ValueError("Returned image output is not bound to selected payload")
        bound_descriptions.append({**result, "part_id": identity[0]})
    descriptions = bound_descriptions

    if (conn is not None and descriptions and len(descriptions) == len(image_attachments)
            and len({desc["part_id"] for desc in descriptions}) == len(image_attachments)
            and (fetched.source_coverage is None or fetched.source_coverage.image_selection_complete)):

        now = datetime.now(timezone.utc).isoformat()
        for desc in descriptions:
            insert_evidence(
                conn,
                project_id=project_id,
                evidence_type="attachment",
                content_summary=desc["description"],
                evidence_at=now,
                content_text=desc["description"],
                source_ref=f"attachment:{fetched.email_id}:{desc['path']}",
            )

    logger.info(
        "Processed %d image attachments for email %s (project %s)",
        len(descriptions), fetched.email_id, project_id,
    )
    return descriptions


# Backward-compatible alias for tests and legacy imports
_process_image_attachments = process_image_attachments
