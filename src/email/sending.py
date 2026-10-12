"""
Email Sending Layer — adapter pattern for multiple email backends.
Supports: SMTP, Gmail API (future), MS Graph (future), SendGrid (future).
Phase 2: SMTP only. Includes SenderIdentity, fallback chain, retry with backoff.
"""

import sqlite3
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
BACKOFF_BASE = 1  # seconds: 1, 2, 4


@dataclass
class SenderIdentity:
    """Structured sender identity (Blueprint Section 28)."""
    identity_id: str
    display_name: str
    email_address: str
    send_method: str = "smtp"  # smtp | gmail | msgraph
    scope: str = "default"  # default | project_specific
    project_id: str | None = None
    is_default: bool = False


@dataclass
class SendResult:
    """Result of an email send attempt."""
    success: bool
    message_id: str | None = None
    error: str | None = None
    attempts: int = 1
    raw_message: bytes | None = None
    retryable: bool = True  # Only proven pre-acceptance failures may retry.
    uncertain: bool = False  # Provider acceptance cannot be ruled out.
    recipient_rejections: dict[str, str] | None = None


import re as _re

_SUBJECT_PREFIX_RE = _re.compile(
    r"^(\s*(re|fwd?|aw|回覆|转发|轉寄)\s*[:：]\s*)+", _re.IGNORECASE
)


def _strip_subject_prefixes(subject: str) -> str:
    """Strip Re:/Fwd:/AW:/回覆:/轉寄: prefixes — result is Exchange Thread-Topic."""
    return _SUBJECT_PREFIX_RE.sub("", subject).strip()


def resolve_sender(
    conn: sqlite3.Connection | None,
    project_id: str | None,
    config: dict,
) -> SenderIdentity:
    """
    Resolve sender identity using fallback chain:
    project_specific → default → halt.

    Raises ValueError if no sender identity can be resolved.
    """
    identities = config.get("sender_identities") or []

    # 1. Project-specific identity
    if project_id and identities:
        for item in identities:
            if item.get("scope") == "project_specific" and item.get("project_id") == project_id:
                email_addr = item.get("email_address")
                if not email_addr:
                    raise ValueError(
                        f"sender_identities entry for project {project_id} missing email_address"
                    )
                return SenderIdentity(
                    identity_id=item.get("identity_id", "project_specific"),
                    display_name=item.get("display_name", email_addr),
                    email_address=email_addr,
                    send_method=item.get("send_method", "smtp"),
                    scope="project_specific",
                    project_id=project_id,
                    is_default=False,
                )

    # 2. Explicit default from sender_identities list
    for item in identities:
        if item.get("is_default") or item.get("scope") == "default":
            email_addr = item.get("email_address")
            if not email_addr:
                raise ValueError("default sender_identities entry missing email_address")
            return SenderIdentity(
                identity_id=item.get("identity_id", "default"),
                display_name=item.get("display_name", email_addr),
                email_address=email_addr,
                send_method=item.get("send_method", "smtp"),
                scope="default",
                is_default=True,
            )

    # 3. Fallback to basic config-level from_email
    default_email = config.get("from_email") or config.get("smtp_username", "")
    if default_email:
        return SenderIdentity(
            identity_id="default",
            display_name=config.get("from_name", default_email),
            email_address=default_email,
            send_method="smtp",
            scope="default",
            is_default=True,
        )

    # 4. Halt — no sender identity found
    raise ValueError(
        "No sender identity configured: set email.from_email or email.sender_identities in config.json"
    )


class EmailSender:
    """
    Adapter for sending emails through configurable backends.
    Phase 2 implements SMTP. Other backends can be added later.
    """

    def __init__(self, backend: str = "smtp", config: dict | None = None):
        self.backend = backend
        self.config = config or {}

    def send(
        self,
        to_email: str | list[str],
        subject: str,
        body: str,
        *,
        body_html: str | None = None,
        cc: str | list[str] | None = None,
        bcc: str | list[str] | None = None,
        in_reply_to: str | None = None,
        references: str | None = None,
        message_id: str | None = None,
        from_email: str | None = None,
        attachments: list[dict] | None = None,
    ) -> SendResult:
        """
        Send an email through the configured backend.
        Retries up to 3 times with exponential backoff on failure.
        """
        if self.backend == "smtp":
            return self._send_smtp_with_retry(
                to_email, subject, body,
                body_html=body_html, cc=cc, bcc=bcc,
                in_reply_to=in_reply_to, references=references,
                message_id=message_id, from_email=from_email,
                attachments=attachments,
            )
        elif self.backend == "console":
            return self._send_console(to_email, subject, body)
        elif self.backend == "msgraph":
            return self._send_graph(
                to_email, subject, body,
                body_html=body_html, cc=cc, bcc=bcc,
                in_reply_to=in_reply_to, references=references,
            )
        else:
            return SendResult(success=False, error=f"Unknown backend: {self.backend}")

    def _send_smtp_with_retry(
        self, to_email: str, subject: str, body: str, **kwargs
    ) -> SendResult:
        """Send via SMTP with retry and exponential backoff.

        Only retries errors that occurred *before* the server accepted the
        message (connect/auth). After ``send_message()`` is reached, any
        exception is treated as non-retryable to avoid duplicate delivery.
        """
        last_error = ""
        for attempt in range(1, MAX_RETRIES + 1):
            result = self._send_smtp(to_email, subject, body, **kwargs)
            result.attempts = attempt
            if result.success:
                return result
            last_error = result.error or "Unknown error"
            if not result.retryable:
                logger.error(
                    "SMTP non-retryable failure after send_message reached: %s",
                    last_error,
                )
                return result
            if attempt < MAX_RETRIES:
                wait = BACKOFF_BASE * (2 ** (attempt - 1))
                logger.warning(
                    "SMTP send attempt %d/%d failed: %s — retrying in %ds",
                    attempt, MAX_RETRIES, last_error, wait,
                )
                time.sleep(wait)
        logger.error("SMTP send failed after %d attempts: %s", MAX_RETRIES, last_error)
        return SendResult(success=False, error=last_error, attempts=MAX_RETRIES)

    def _send_smtp(
        self, to_email: str | list[str], subject: str, body: str, **kwargs
    ) -> SendResult:
        """Send multipart/alternative email via SMTP."""
        import smtplib
        from email.mime.application import MIMEApplication

        host = self.config.get("smtp_host", "localhost")
        port = self.config.get("smtp_port", 587)
        username = self.config.get("smtp_username", "")
        password = self.config.get("smtp_password", "")
        use_tls = self.config.get("smtp_tls", True)
        from_email = kwargs.get("from_email") or self.config.get("from_email", username)

        def _addresses(value):
            if not value:
                return []
            if isinstance(value, str):
                from email.utils import getaddresses
                return [address for _name, address in getaddresses([value.replace(";", ",")]) if address]
            return [str(address) for address in value if address]

        to_addresses = _addresses(to_email)
        cc_addresses = _addresses(kwargs.get("cc"))
        bcc_addresses = _addresses(kwargs.get("bcc"))
        if self.config.get("self_bcc") and from_email and from_email not in bcc_addresses:
            bcc_addresses.append(from_email)

        msg = MIMEMultipart("mixed")
        msg["From"] = from_email
        msg["To"] = ", ".join(to_addresses)
        msg["Subject"] = subject
        msg["Date"] = __import__("email.utils", fromlist=["formatdate"]).formatdate(localtime=True)
        if cc_addresses:
            msg["Cc"] = ", ".join(cc_addresses)
        if bcc_addresses:
            msg["Bcc"] = ", ".join(bcc_addresses)
        if kwargs.get("message_id"):
            msg["Message-ID"] = kwargs["message_id"]
        if kwargs.get("in_reply_to"):
            msg["In-Reply-To"] = kwargs["in_reply_to"]
            msg["References"] = kwargs.get("references") or kwargs["in_reply_to"]
            logger.info(
                "Threading headers set -- In-Reply-To: %s, References: %s",
                kwargs["in_reply_to"], msg["References"],
            )
        msg["Thread-Topic"] = _strip_subject_prefixes(subject)

        alternative = MIMEMultipart("alternative")
        alternative.attach(MIMEText(body or "", "plain", "utf-8"))
        if kwargs.get("body_html"):
            alternative.attach(MIMEText(kwargs["body_html"], "html", "utf-8"))
        msg.attach(alternative)

        for att in (kwargs.get("attachments") or []):
            content = att.get("content")
            if content is None:
                att_path = Path(att["path"])
                if not att_path.exists():
                    raise FileNotFoundError(f"Attachment missing: {att.get('filename')}")
                content = att_path.read_bytes()
            part = MIMEApplication(content, Name=att["filename"])
            part["Content-Disposition"] = f'attachment; filename="{att["filename"]}"'
            msg.attach(part)

        send_message_reached = False
        try:
            with smtplib.SMTP(host, port, timeout=30) as server:
                if use_tls:
                    server.starttls()
                if username and password:
                    server.login(username, password)
                # Once this call begins, a lost reply cannot prove the MTA did
                # not accept the message. Never issue an automatic replay.
                send_message_reached = True
                rejected = server.send_message(
                    msg,
                    from_addr=from_email,
                    to_addrs=[*to_addresses, *cc_addresses, *bcc_addresses],
                )
                if rejected:
                    return SendResult(
                        success=False,
                        error="SMTP recipient refusal; delivery requires reconciliation",
                        retryable=False,
                        uncertain=True,
                        recipient_rejections={
                            str(address): str(reply) for address, reply in rejected.items()
                        },
                    )
                return SendResult(
                    success=True,
                    message_id=msg.get("Message-ID", ""),
                    raw_message=msg.as_bytes(),
                )
        except Exception as exc:
            logger.error("SMTP send failed: %s", exc)
            return SendResult(
                success=False,
                error=str(exc),
                retryable=not send_message_reached,
                uncertain=send_message_reached,
            )

    def _send_console(self, to_email: str, subject: str, body: str) -> SendResult:
        """Console backend — logs the email instead of sending. Useful for testing."""
        logger.info("=== EMAIL (console backend) ===")
        logger.info("To: %s", to_email)
        logger.info("Subject: %s", subject)
        logger.info("Body:\n%s", body)
        logger.info("===============================")
        return SendResult(success=True, message_id=f"console-{datetime.now(timezone.utc).isoformat()}")

    def _send_graph(
        self, to_email: str, subject: str, body: str, **kwargs
    ) -> SendResult:
        """Send via Microsoft Graph API (app-only client_credentials flow)."""
        request_started = False
        try:
            from src.email.graph_client import GraphClient
            gc = GraphClient(
                tenant_id=self.config["tenant_id"],
                client_id=self.config["client_id"],
                client_secret=self.config["client_secret"],
                user_email=self.config["user_email"],
            )
            request_started = True
            message_id = gc.send_message(
                to=to_email,
                subject=subject,
                body=body,
                body_html=kwargs.get("body_html"),
                cc=kwargs.get("cc"),
                bcc=kwargs.get("bcc"),
                in_reply_to=kwargs.get("in_reply_to"),
                references=kwargs.get("references"),
            )
            return SendResult(success=True, message_id=message_id)
        except Exception as e:
            logger.error("Graph send failed: %s", e)
            return SendResult(
                success=False, error=str(e), uncertain=request_started,
                retryable=not request_started,
            )


def _imap_append_to_sent(raw_message: bytes, imap_config: dict) -> None:
    """
    Append the sent message to the IMAP 'Sent Items' folder
    so it appears in Outlook.

    Best-effort — failure is logged but does not fail the send.
    Tries the configured folder name first, then falls back to discovering
    the correct Sent Items folder via IMAP LIST (handles Exchange variants).
    """
    import imaplib

    host = imap_config["host"]
    port = imap_config.get("port", 993)
    username = imap_config["username"]
    password = imap_config["password"]
    folder = imap_config.get("sent_folder", "Sent Items")
    use_ssl = imap_config.get("use_ssl", True)

    mail = imaplib.IMAP4_SSL(host, port) if use_ssl else imaplib.IMAP4(host, port)
    mail.login(username, password)
    try:
        status, _ = mail.append(folder, "\\Seen", None, raw_message)
        if status != "OK":
            # Folder name may differ — discover via LIST and retry
            _discovered = _find_sent_folder(mail)
            if _discovered and _discovered != folder:
                logger.info("IMAP append: retrying with discovered folder %r", _discovered)
                mail.append(_discovered, "\\Seen", None, raw_message)
    finally:
        mail.logout()


def _find_sent_folder(mail) -> str | None:
    """List IMAP folders and return the first one whose name contains 'sent'."""
    import re
    try:
        status, folders = mail.list()
        if status != "OK":
            return None
        for item in folders:
            name = item.decode() if isinstance(item, bytes) else item
            # LIST response format: '(\Flags) "delim" "Folder Name"' or '... FolderName'
            m = re.search(r'"([^"]+)"\s*$|(\S+)\s*$', name)
            if m:
                candidate = m.group(1) or m.group(2) or ""
                if "sent" in candidate.lower():
                    return candidate
    except Exception:
        pass
    return None


def record_sent_email(
    conn: sqlite3.Connection,
    draft_id: str,
    message_id: str | None,
    to_email: str,
    subject: str,
    *,
    ticket_id: str | None = None,
    snapshot: dict | None = None,
    sender_email: str | None = None,
    commit: bool = True,
) -> None:
    """Record an outbound message using the immutable delivery snapshot."""
    from src.email.recipients import envelope_to_legacy, normalize_envelope, parse_recipient_list

    now = datetime.now(timezone.utc).isoformat()
    delivery_context = (snapshot or {}).get("delivery_context") or {}
    reply_to_email_id = (
        delivery_context.get("reply_to_email_id")
        if delivery_context.get("delivery_mode") == "reply"
        else None
    )
    if not reply_to_email_id and snapshot is None:
        legacy = conn.execute(
            "SELECT context_packet_ref FROM drafts WHERE draft_id=?",
            (draft_id,),
        ).fetchone()
        reply_to_email_id = legacy[0] if legacy else None

    thread_fp = None
    thread_id = None
    references_chain = None
    if reply_to_email_id:
        source = conn.execute(
            """SELECT thread_fp, thread_id, references_chain
               FROM processed_emails WHERE email_id=?""",
            (reply_to_email_id,),
        ).fetchone()
        if source:
            thread_fp = source["thread_fp"]
            thread_id = source["thread_id"]
            existing = (source["references_chain"] or "").strip()
            references_chain = (
                f"{existing} {reply_to_email_id}".strip()
                if existing
                else reply_to_email_id
            )

    if snapshot is not None:
        envelope = normalize_envelope(snapshot.get("envelope"))
    else:
        envelope = normalize_envelope({"to": parse_recipient_list(to_email)})
    legacy_to, legacy_cc, _legacy_bcc = envelope_to_legacy(envelope)

    if not sender_email:
        from config.settings import CONFIG

        if snapshot is not None:
            identity_id = (snapshot.get("envelope") or {}).get("sender_identity_id")
            identity = next(
                (
                    item
                    for item in email_config_dict(CONFIG).get("sender_identities") or []
                    if item.get("identity_id") == identity_id
                ),
                None,
            )
            sender_email = (identity or {}).get("email_address")
        sender_email = sender_email or email_config_dict(CONFIG).get("from_email") or ""

    conn.execute(
        """INSERT OR IGNORE INTO processed_emails
           (email_id, project_id, processed_at, received_at, result, subject,
            sender_email, direction, thread_fp, thread_id, in_reply_to,
            references_chain, cc, to_recipients_json, cc_recipients_json,
            bcc_recipients_json, ticket_id, body_snippet)
           VALUES (?, (SELECT project_id FROM drafts WHERE draft_id = ?), ?, ?, ?,
                   ?, ?, 'outbound', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            message_id or draft_id,
            draft_id,
            now,
            now,
            "sent",
            subject,
            sender_email,
            thread_fp,
            thread_id,
            reply_to_email_id,
            references_chain,
            legacy_cc,
            json.dumps(envelope["to"], ensure_ascii=False),
            json.dumps(envelope["cc"], ensure_ascii=False),
            json.dumps(envelope["bcc"], ensure_ascii=False),
            ticket_id,
            ((snapshot or {}).get("content") or {}).get("body_text"),
        ),
    )
    if commit:
        conn.commit()


def record_successful_send_bookkeeping(
    conn: sqlite3.Connection,
    *,
    draft_id: str,
    message_id: str,
    to_email: str,
    subject: str,
    snapshot: dict | None = None,
    sender_email: str | None = None,
    commit: bool = True,
) -> None:
    """Persist local records that follow provider acceptance.

    Callers must persist provider acceptance before invoking this helper. This
    bookkeeping is intentionally reusable by both legacy and outbox delivery
    paths and must never be used as a reason to resend an accepted message.
    """
    from src.email.draft_session import get_draft

    draft = get_draft(conn, draft_id)
    if draft is None:
        raise ValueError("Draft not found during successful-send bookkeeping")
    ticket_id = draft.get("ticket_id")
    record_sent_email(
        conn,
        draft_id,
        message_id,
        to_email,
        subject,
        ticket_id=ticket_id,
        snapshot=snapshot,
        sender_email=sender_email,
        commit=False,
    )
    if ticket_id:
        from src.repositories.ticket_evidence_repo import insert_ticket_evidence
        from src.repositories.ticket_repo import get_ticket, update_ticket_fields

        ticket_obj = get_ticket(conn, ticket_id)
        category = getattr(ticket_obj.category, "value", ticket_obj.category) if ticket_obj else None
        post_send_status = "closed" if category == "outreach" else "waiting_external"
        update_ticket_fields(
            conn,
            ticket_id,
            status=post_send_status,
            reply_draft_text=None,
        )
        evidence_summary = (
            f"Outreach sent to {to_email}: {subject or '(no subject)'}"
            if category == "outreach"
            else f"Outbound: {subject or 'Reply'}"
        )
        insert_ticket_evidence(
            conn,
            ticket_id,
            "email",
            evidence_summary,
            source_ref=message_id,
        )

    from src.learning.evaluation.producer_outcomes import attribute_draft_outcome

    attribute_draft_outcome(
        conn,
        draft_id=draft_id,
        draft_version=int(draft.get("draft_version") or 1),
        outcome_type="send",
        idempotency_key=f"send:{message_id}",
        actor_id="email_send",
    )
    if commit:
        conn.commit()


def run_post_send_hooks(
    conn: sqlite3.Connection,
    *,
    draft_id: str,
    to_email: str,
) -> None:
    """Run best-effort learning and project-state hooks after a durable send."""
    from src.email.draft_session import get_draft

    sent_row = get_draft(conn, draft_id)
    if sent_row is None:
        logger.warning("Post-send hooks skipped because draft %s no longer exists", draft_id)
        return
    ticket_id = sent_row.get("ticket_id")
    if ticket_id:
        logger.info("Ticket %s updated after draft %s sent", ticket_id, draft_id)

    try:
        from src.learning.governance_flags import candidate_only
        from src.learning.ingress import coordinate_ingress
        from src.learning.jobs.sources import enqueue_post_send_learning

        def legacy_post_send():
            from src.advisory.passive_learner import learn_from_sent_email
            from src.repositories.ticket_repo import get_open_tickets_for_project

            receipts = []
            project_id = sent_row.get("project_id")
            if project_id:
                for ticket in get_open_tickets_for_project(conn, project_id)[:1]:
                    receipts.append(learn_from_sent_email(conn, sent_row, ticket.ticket_id))
            return {"invoked": bool(receipts)}

        receipt = coordinate_ingress(
            conn,
            source_type="post_send",
            source_id=draft_id,
            operator_id=str(sent_row.get("operator_id") or "default_operator"),
            project_id=sent_row.get("project_id"),
            v2_callback=lambda: enqueue_post_send_learning(conn, sent_row),
            legacy_callback=legacy_post_send,
            candidate_only=candidate_only(),
        )
        if receipt.status != "succeeded":
            logger.warning("Post-send learning ingress %s for draft %s", receipt.status, draft_id)
    except Exception as exc:
        logger.warning("Post-send learning ingress skipped for draft %s: %s", draft_id, exc)

    try:
        project_id = sent_row.get("project_id")
        if project_id:
            from src.write_layer.write_helpers import safe_write_project

            safe_write_project(
                conn,
                project_id,
                {"ball_with": "External"},
                actor="email_send",
                reason=f"Email sent to {to_email}",
            )
            logger.info("ball_with set to External for project %s", project_id)
    except Exception:
        logger.warning("ball_with update skipped for draft %s", draft_id)


def email_config_dict(config) -> dict:
    """Convert AppConfig.email to a plain dict for EmailSender / execute_draft_send."""
    ec = config.email
    return {
        "backend": ec.backend,
        "smtp_host": ec.smtp_host,
        "smtp_port": ec.smtp_port,
        "smtp_username": ec.smtp_username,
        "smtp_password": ec.smtp_password,
        "smtp_tls": ec.smtp_tls,
        "from_email": ec.from_email,
        "from_name": ec.from_name,
        "self_bcc": ec.self_bcc,
        "sender_identities": ec.sender_identities,
    }


def imap_config_dict(config) -> dict | None:
    """Return IMAP config dict for sent-folder append, or None if not usable.

    Returns a dict when IMAP credentials are configured and append_sent is True,
    regardless of whether inbox polling (imap.enabled) is active.
    """
    imap = config.email.imap
    has_credentials = bool(imap.host and imap.username)
    if not has_credentials:
        return None
    if not (imap.enabled or imap.append_sent):
        return None
    return {
        "host": imap.host,
        "port": imap.port,
        "username": imap.username,
        "password": imap.password,
        "use_ssl": imap.use_ssl,
        "sent_folder": imap.sent_folder,
    }


def send_workspace_snapshot(
    conn: sqlite3.Connection,
    snapshot: dict,
    email_config: dict,
    *,
    imap_config: dict | None = None,
) -> SendResult:
    """Send one immutable workspace snapshot without reading mutable draft content."""
    import email.utils as email_utils

    from src.email.recipients import canonical_email, normalize_envelope

    envelope = normalize_envelope(snapshot.get("envelope"))
    identity_id = (snapshot.get("envelope") or {}).get("sender_identity_id")
    identities = email_config.get("sender_identities") or []
    configured_backend = email_config.get("backend", "smtp")
    selected = next(
        (item for item in identities if (item.get("identity_id") or canonical_email(item.get("email_address"))) == identity_id),
        None,
    )
    if selected:
        from_email = selected.get("email_address")
        identity_backend = selected.get("send_method")
    else:
        try:
            fallback = resolve_sender(conn, snapshot.get("project_id"), email_config)
        except ValueError as exc:
            return SendResult(success=False, error=str(exc))
        if identity_id and fallback.identity_id != identity_id:
            return SendResult(success=False, error="Sender identity is no longer allowlisted")
        from_email = fallback.email_address
        identity_backend = fallback.send_method

    # Console is an explicit dry-run transport and must override persisted identity
    # defaults. Production transports continue to honor the allowlisted identity.
    backend = "console" if configured_backend == "console" else (identity_backend or configured_backend)

    reply_context = snapshot.get("delivery_context") or {}
    in_reply_to = None
    references = None
    if reply_context.get("delivery_mode") == "reply":
        reply_id = reply_context.get("reply_to_email_id")
        row = conn.execute(
            "SELECT email_id, references_chain FROM processed_emails WHERE email_id=?",
            (reply_id,),
        ).fetchone()
        if not row:
            return SendResult(success=False, error="Confirmed reply anchor no longer exists")
        in_reply_to = row["email_id"]
        existing = (row["references_chain"] or "").strip()
        references = f"{existing} {in_reply_to}".strip() if existing else in_reply_to

    domain = from_email.split("@")[-1] if from_email and "@" in from_email else "localhost"
    generated_id = email_utils.make_msgid(domain=domain)
    content = snapshot.get("content") or {}
    sender = EmailSender(backend=backend, config=email_config)
    send_started = False

    def send_outside_writer() -> SendResult:
        nonlocal send_started
        send_started = True
        return sender.send(
            to_email=[item["email"] for item in envelope["to"]],
            cc=[item["email"] for item in envelope["cc"]],
            bcc=[item["email"] for item in envelope["bcc"]],
            subject=content.get("subject") or "",
            body=content.get("body_text") or "",
            body_html=content.get("body_html") or None,
            in_reply_to=in_reply_to,
            references=references,
            message_id=generated_id,
            from_email=from_email,
            attachments=snapshot.get("attachments") or [],
        )

    try:
        from src.db.connection import run_external_io

        result = run_external_io(conn, send_outside_writer)
    except Exception as exc:
        logger.exception("Immutable draft snapshot send returned no durable provider result")
        return SendResult(
            success=False, error=str(exc), uncertain=send_started,
            retryable=not send_started,
        )

    if result.success:
        result.message_id = result.message_id or generated_id
        if imap_config and result.raw_message and backend == "smtp":
            try:
                from src.db.connection import run_external_io
                run_external_io(conn, lambda: _imap_append_to_sent(result.raw_message, imap_config))
            except Exception as exc:
                logger.warning("IMAP append failed after workspace send: %s", exc)
    return result


def execute_draft_send(
    conn: sqlite3.Connection | None,
    draft_id: str,
    email_config: dict,
    imap_config: dict | None = None,
    *,
    actor_user_id: str = "system:legacy-send",
    confirmed_warnings: list[str] | None = None,
    action_intent: dict | None = None,
) -> SendResult:
    """Compatibility send entry routed through the mandatory gate and outbox.

    Legacy callers remain synchronous by using a zero-second Undo Send window,
    but they no longer read mutable draft fields or derive reply headers from
    ``context_packet_ref``.
    """
    from src.email.draft_send_command import queue_approved_draft_send
    from src.email.outbox import dispatch_outbox

    def _execute(c: sqlite3.Connection) -> SendResult:
        queued, rejection = queue_approved_draft_send(
            c,
            draft_id,
            email_config,
            actor_user_id=actor_user_id,
            confirmed_warnings=confirmed_warnings,
            action_intent=action_intent,
            commit=True,
        )
        if rejection is not None:
            return rejection
        assert queued is not None
        return dispatch_outbox(
            c,
            queued["outbox_id"],
            email_config=email_config,
            imap_config=imap_config,
        )

    if conn is not None:
        return _execute(conn)
    from src.db.connection import scoped_write

    with scoped_write() as c:
        return _execute(c)
