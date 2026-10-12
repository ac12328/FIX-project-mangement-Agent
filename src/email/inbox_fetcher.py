"""
Email inbox fetcher — backend-swappable Protocol + IMAP implementation.

InboxFetcher (Protocol)
  └── ImapInboxFetcher   ← stdlib imaplib only (no external deps)
       future: GraphInboxFetcher ← swap in without touching any other module

Usage:
    from src.email.inbox_fetcher import ImapInboxFetcher
    fetcher = ImapInboxFetcher.from_config(CONFIG.email.imap)
    result = fetcher.fetch_inbox(conn)
    for email in result.fetched: ...
"""

import hashlib
import html
import imaplib
import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Protocol, runtime_checkable

from src.email.source_coverage import EmailSourceCoverage, inventory_source

logger = logging.getLogger(__name__)

_IMAP_SEARCH_ALL = "ALL"
_IMAP_SEARCH_UNSEEN = "UNSEEN"
_DEFAULT_UID_WATERMARK_FILE = Path("data/imap_inbox_uid_watermark.txt")

# ── Module-level UID caches (persist across poll cycles within process) ──
_MAX_CACHED_UIDS = 10_000
_processed_uids: set[int] = set()      # UIDs already in processed_emails
_failed_parse_uids: set[int] = set()   # UIDs that failed parsing (encoding errors etc.)
_cache_loaded: bool = False             # Whether disk cache has been loaded this process

_UID_CACHE_FILE = Path("data/imap_uid_cache.json")


def _cache_uid(uid: int, cache: set[int]) -> None:
    """Add a UID to a cache set, evicting oldest entries if over limit."""
    if len(cache) >= _MAX_CACHED_UIDS:
        # Evict ~10% of lowest UIDs (oldest)
        to_remove = sorted(cache)[:_MAX_CACHED_UIDS // 10]
        cache.difference_update(to_remove)
    cache.add(uid)


def _load_uid_cache() -> None:
    """Load UID caches from disk on first poll. Idempotent."""
    global _cache_loaded
    if _cache_loaded:
        return
    _cache_loaded = True
    try:
        if _UID_CACHE_FILE.exists():
            data = json.loads(_UID_CACHE_FILE.read_text(encoding="utf-8"))
            _processed_uids.update(data.get("processed", []))
            _failed_parse_uids.update(data.get("failed", []))
            logger.info(
                "Loaded UID cache from disk: %d processed, %d failed",
                len(_processed_uids), len(_failed_parse_uids),
            )
    except Exception as exc:
        logger.warning("Failed to load UID cache from disk: %s", exc)


def cache_processed_uid(uid: int) -> None:
    """Mark an IMAP UID as processed so SINCE sweeps skip re-fetching it."""
    _cache_uid(uid, _processed_uids)


def save_uid_cache() -> None:
    """Persist UID caches to disk. Call after each successful poll cycle."""
    try:
        _UID_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "processed": sorted(_processed_uids)[-_MAX_CACHED_UIDS:],
            "failed": sorted(_failed_parse_uids),
        }
        _UID_CACHE_FILE.write_text(json.dumps(data), encoding="utf-8")
    except Exception as exc:
        logger.warning("Failed to save UID cache to disk: %s", exc)


def reset_uid_caches() -> None:
    """Reset UID caches — for testing or manual reset."""
    global _cache_loaded
    _processed_uids.clear()
    _failed_parse_uids.clear()
    _cache_loaded = False


@dataclass
class AttachmentData:
    """One email attachment (image, PDF, etc.)."""
    filename: str
    content_type: str       # e.g. "image/png"
    data: bytes
    size_bytes: int
    mime_part_id: str | None = None
    payload_sha256: str | None = None


@dataclass
class FetchedEmail:
    """Normalised representation of one email message."""
    email_id: str           # Message-ID header or "hash:<sha256[:16]>" fallback
    sender_email: str
    subject: str
    body: str               # plain text only
    received_at: str        # UTC ISO 8601
    folder: str             # "inbox" | "sent"
    in_reply_to: str | None = None
    references: str | None = None
    cc: str | None = None
    bcc: str | None = None
    to_emails: list[str] = field(default_factory=list)
    to_recipients: list[dict] = field(default_factory=list)
    cc_recipients: list[dict] = field(default_factory=list)
    bcc_recipients: list[dict] = field(default_factory=list)
    uid: int | None = None
    attachments: list[AttachmentData] = field(default_factory=list)
    sent_at: str | None = None  # Authored timestamp; never substituted with processing time.
    source_coverage: EmailSourceCoverage | None = None
    raw_source: bytes | None = field(default=None, repr=False)


@dataclass
class FetchResult:
    """Result of one fetch_inbox() or fetch_sent() call."""
    fetched: list[FetchedEmail] = field(default_factory=list)
    skipped_count: int = 0    # already in processed_emails
    error: str | None = None
    max_uid: int | None = None  # highest UID fetched — caller should advance watermark after processing
    uid_validity: int | None = None  # mailbox UID generation for durable re-fetch


class SourceUnavailable(RuntimeError):
    """A persisted IMAP source can no longer be verified for safe processing."""


def _read_uid_validity(mail) -> int | None:
    try:
        kind, values = mail.response("UIDVALIDITY")
        if kind != "UIDVALIDITY" or not values:
            return None
        value = int(values[0])
        return value if value > 0 else None
    except (TypeError, ValueError):
        return None


@runtime_checkable
class InboxFetcher(Protocol):
    """Backend-swappable interface for email inbox access."""

    def fetch_inbox(
        self, conn: sqlite3.Connection, *, mark_seen: bool = False
    ) -> FetchResult: ...

    def fetch_sent(self, conn: sqlite3.Connection) -> FetchResult: ...

    def health_check(self) -> bool: ...


def _html_to_text(html_text: str) -> str:
    """Convert HTML email body to readable plain text fallback."""
    try:
        if not html_text:
            return ""
        
        # a. Replace <br>, <p>, <div> variants with \n
        text = re.sub(r'(?i)<(br|/p|/div)[^>]*>', '\n', html_text)
        text = re.sub(r'(?i)<(p|div)[^>]*>', '\n', text)
        
        # b. Strip all remaining HTML tags
        text = re.sub(r'<[^>]+>', '', text)
        
        # c. Decode HTML entities
        text = html.unescape(text)
        
        # d. Collapse 3+ consecutive newlines into \n\n
        text = re.sub(r'\n{3,}', '\n\n', text)
        
        # e. Strip leading/trailing whitespace
        return text.strip()
    except Exception:
        return ""


def _extract_plain_text(msg) -> str:
    """Extract plain-text body from a parsed email.Message object.

    Compatible with both the legacy email.Message API (get_payload) and
    the newer EmailMessage API (get_content / policy.default).
    """
    def _get_text(part) -> str:
        if hasattr(part, "get_content"):
            return part.get_content() or ""
        payload = part.get_payload(decode=True)
        if payload:
            charset = part.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")
        return ""

    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                return _get_text(part)
        # Fallback to HTML if no plain text part exists
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                html_text = _get_text(part)
                if html_text:
                    fallback = _html_to_text(html_text)
                    if fallback:
                        return fallback
    else:
        if msg.get_content_type() == "text/plain":
            return _get_text(msg)
        if msg.get_content_type() == "text/html":
            html_text = _get_text(msg)
            if html_text:
                fallback = _html_to_text(html_text)
                if fallback:
                    return fallback
    return ""


# ------------------------------------------------------------------
# Attachment extraction (images + document metadata/bytes)
# ------------------------------------------------------------------

_MAX_IMAGE_BYTES = 5 * 1024 * 1024   # 5 MB per image
_MAX_DOC_BYTES = 10 * 1024 * 1024    # 10 MB per non-image attachment
_MAX_IMAGES_PER_EMAIL = 5
_MAX_ATTACHMENTS_PER_EMAIL = 15
_IMAGE_PREFIX = "image/"
_SKIP_CONTENT_TYPES = frozenset({
    "text/plain",
    "text/html",
    "multipart/mixed",
    "multipart/alternative",
    "multipart/related",
    "multipart/signed",
    "message/rfc822",
})


def _is_attachment_part(part) -> bool:
    """True if part looks like a file attachment (not body)."""
    filename = part.get_filename()
    if filename:
        return True
    disp = (part.get_content_disposition() or "").lower()
    return disp in ("attachment", "inline")


def _extract_image_attachments(msg) -> list[AttachmentData]:
    """Backward-compatible wrapper — usable image payloads only."""
    return [
        a
        for a in _extract_attachments(msg)
        if a.content_type.startswith(_IMAGE_PREFIX) and a.data
    ]


def _extract_attachments(msg) -> list[AttachmentData]:
    """Extract attachments from a parsed email message.

    - Images: full bytes (cap 5 MB each, max 5 images) for vision pipeline
    - PDF/DOCX and other files: full bytes when under size limit (for text extract)
    - Always records filename + content_type so analysis can classify evidence
      (e.g. conformance report vs network screenshot)
    """
    attachments: list[AttachmentData] = []
    image_count = 0
    if not msg.is_multipart():
        return attachments

    for part_index, part in enumerate(msg.walk()):
        ct = (part.get_content_type() or "").lower()
        if ct in _SKIP_CONTENT_TYPES or ct.startswith("multipart/"):
            continue
        if not _is_attachment_part(part) and not ct.startswith(_IMAGE_PREFIX):
            continue

        filename = part.get_filename() or ""
        payload = part.get_payload(decode=True) or b""
        size = len(payload)

        is_image = ct.startswith(_IMAGE_PREFIX)
        if is_image:
            if image_count >= _MAX_IMAGES_PER_EMAIL:
                continue
            if size > _MAX_IMAGE_BYTES:
                logger.debug("Skipping oversized image attachment (%d bytes)", size)
                # Still record metadata without payload
                attachments.append(AttachmentData(
                    filename=filename or f"image_{image_count}.bin",
                    content_type=ct,
                    data=b"",
                    size_bytes=size,
                    mime_part_id=f"mime-{part_index}",
                    payload_sha256=hashlib.sha256(payload).hexdigest(),
                ))
                image_count += 1
                continue
            if not filename:
                filename = f"image_{image_count}.{ct.split('/')[-1] or 'bin'}"
            attachments.append(AttachmentData(
                filename=filename,
                content_type=ct,
                data=payload,
                size_bytes=size,
                    mime_part_id=f"mime-{part_index}",
                    payload_sha256=hashlib.sha256(payload).hexdigest(),
            ))
            image_count += 1
        else:
            if not filename:
                filename = f"attachment_{len(attachments)}.bin"
            # Keep bytes for extractable docs; metadata-only if too large
            data = payload if size and size <= _MAX_DOC_BYTES else b""
            if size > _MAX_DOC_BYTES:
                logger.debug(
                    "Attachment %s too large (%d bytes) — metadata only",
                    filename, size,
                )
            attachments.append(AttachmentData(
                filename=filename,
                content_type=ct or "application/octet-stream",
                data=data,
                size_bytes=size,
                    mime_part_id=f"mime-{part_index}",
                    payload_sha256=hashlib.sha256(payload).hexdigest(),
            ))

        if len(attachments) >= _MAX_ATTACHMENTS_PER_EMAIL:
            break

    return attachments


class ImapInboxFetcher:
    """
    IMAP email fetcher using stdlib imaplib.
    No external dependencies — imaplib, email, hashlib are all stdlib.

    Creates one IMAP connection per call, closes it immediately after.
    This prevents stale connection issues on long-running bots.
    """

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        use_ssl: bool = True,
        inbox_folder: str = "INBOX",
        sent_folder: str = "Sent Items",
        sent_window_hours: int = 24,
        uid_watermark_file: Path | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.use_ssl = use_ssl
        self.inbox_folder = inbox_folder
        self.sent_folder = sent_folder
        self.sent_window_hours = sent_window_hours
        self._uid_watermark_file = uid_watermark_file or _DEFAULT_UID_WATERMARK_FILE

    def _source_identity(self) -> str:
        identity = f"{self.host.lower()}:{self.port}:{self.username.lower()}:{self.inbox_folder}"
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    def _watermark_state(self) -> tuple[int, str | None, int | None]:
        """Legacy numeric marks have no UID generation and are untrusted."""
        try:
            raw = self._uid_watermark_file.read_text(encoding="utf-8").strip()
            try:
                return max(0, int(raw)), None, None
            except ValueError:
                state = json.loads(raw)
                return int(state["uid"]), state["source"], int(state["uid_validity"])
        except (OSError, ValueError, TypeError, KeyError):
            return 0, None, None

    def _read_uid_watermark(self) -> int:
        """Read the last-seen inbox UID from disk; used by legacy callers."""
        return self._watermark_state()[0]

    def _read_uid_watermark_for(self, uid_validity: int | None) -> int:
        uid, source, generation = self._watermark_state()
        if uid_validity is None:
            return uid
        if source == self._source_identity() and generation == uid_validity:
            return uid
        if uid:
            logger.warning("IMAP mailbox identity changed or watermark is unbound; rescan from UID 1")
        _processed_uids.clear()
        _failed_parse_uids.clear()
        return 0

    def _write_uid_watermark(self, uid: int, *, uid_validity: int | None = None) -> None:
        """Persist a generation-bound UID mark; preserve the legacy test API."""
        try:
            self._uid_watermark_file.parent.mkdir(parents=True, exist_ok=True)
            data = str(uid) if uid_validity is None else json.dumps({
                "uid": uid, "source": self._source_identity(), "uid_validity": uid_validity,
            })
            temporary = self._uid_watermark_file.with_name(self._uid_watermark_file.name + ".tmp")
            temporary.write_text(data, encoding="utf-8")
            os.replace(temporary, self._uid_watermark_file)
        except Exception as exc:
            logger.warning("Failed to write UID watermark: %s", exc)

    @classmethod
    def from_config(cls, imap_cfg) -> "ImapInboxFetcher":
        """Construct from an ImapConfig Pydantic model."""
        return cls(
            host=imap_cfg.host,
            port=imap_cfg.port,
            username=imap_cfg.username,
            password=imap_cfg.password,
            use_ssl=imap_cfg.use_ssl,
            inbox_folder=imap_cfg.inbox_folder,
            sent_folder=imap_cfg.sent_folder,
            sent_window_hours=imap_cfg.sent_window_hours,
        )

    # ── Public interface ───────────────────────────────────────────

    def fetch_inbox(
        self,
        conn: sqlite3.Connection,
        *,
        mark_seen: bool = False,
    ) -> FetchResult:
        """
        Fetch new messages from the inbox folder using a UID watermark.

        Uses the last-seen UID stored in data/imap_inbox_uid_watermark.txt to
        fetch all messages with UID > watermark, regardless of read/unread state.
        This prevents missing emails that were opened in another client (e.g.
        Outlook) before the bot polled.

        Deduplicates against processed_emails as a safety net.

        Args:
            conn: SQLite connection for deduplication lookup.
            mark_seen: If True, marks fetched messages as \\Seen on server.

        Returns:
            FetchResult with new messages, skipped count, or error.
        """
        _load_uid_cache()  # warm from disk on first call
        try:
            mail = self._connect()
            try:
                status, _ = mail.select(self.inbox_folder, readonly=not mark_seen)
                if status != "OK":
                    raise Exception(f"Cannot SELECT inbox folder '{self.inbox_folder}' (status={status})")

                uid_validity = _read_uid_validity(mail)
                last_uid = self._read_uid_watermark_for(uid_validity)
                search_criteria = f"UID {last_uid + 1}:*"
                _, uid_data = mail.uid("search", None, search_criteria)
                raw_uids = [int(u) for u in uid_data[0].split() if u]
                # Filter out UIDs ≤ watermark (server may return last message even
                # when no new messages exist, per RFC 3501 UID SEARCH semantics)
                uids = [u for u in raw_uids if u > last_uid]

                # Secondary sweep: catch emails with UIDs below watermark.
                # Exchange can assign non-monotonic UIDs, so a newer email
                # may have a UID lower than the watermark.  SINCE search is
                # cheap (returns UID list only); _fetch_uids dedup skips
                # already-processed messages.
                # Skip SINCE sweep if UID cache is cold (empty) — avoids
                # fetching thousands of already-processed UIDs on first poll
                # after deployment.  The next cycle will have a warm cache.
                if last_uid > 0 and (_processed_uids or _failed_parse_uids):
                    since_date = (
                        datetime.now(timezone.utc) - timedelta(hours=48)
                    ).strftime("%d-%b-%Y")
                    _, since_data = mail.uid("search", None, f"SINCE {since_date}")
                    since_uids = [int(u) for u in since_data[0].split() if u]
                    uid_set = set(uids)
                    missed = [u for u in since_uids if u <= last_uid and u not in uid_set]
                    # Filter out UIDs already known to be processed or unparseable
                    missed = [u for u in missed if u not in _processed_uids and u not in _failed_parse_uids]
                    if missed:
                        logger.info("SINCE sweep found %d UIDs below watermark %d", len(missed), last_uid)
                        uids = missed + uids
                elif last_uid > 0:
                    logger.info(
                        "SINCE sweep skipped (cold UID cache) — will run next cycle after cache warms"
                    )

                result = self._fetch_uids(conn, mail, uids, folder="inbox", mark_seen=mark_seen)
                result.uid_validity = uid_validity

                # Store max UID so caller can advance watermark after processing
                if uids:
                    result.max_uid = max(uids)

                return result
            finally:
                try:
                    mail.logout()
                except Exception:
                    pass
        except Exception as exc:
            logger.error("IMAP fetch_inbox failed: %s", exc)
            return FetchResult(error=str(exc))

    def fetch_sent(self, conn: sqlite3.Connection) -> FetchResult:
        """
        Fetch sent messages within the rolling sent_window_hours window.
        Used to record outbound emails as processed_emails anchors
        so future client replies can be matched back to their project.

        Args:
            conn: SQLite connection for deduplication lookup.

        Returns:
            FetchResult with new sent emails, skipped count, or error.
        """
        try:
            mail = self._connect()
            try:
                status, _ = mail.select(self.sent_folder, readonly=True)
                if status != "OK":
                    raise Exception(f"Cannot SELECT sent folder '{self.sent_folder}' (status={status})")
                since = (
                    datetime.now(timezone.utc) - timedelta(hours=self.sent_window_hours)
                ).strftime("%d-%b-%Y")
                _, uid_data = mail.uid("search", None, f"SINCE {since}")
                uids = [int(u) for u in uid_data[0].split() if u]
                return self._fetch_uids(conn, mail, uids, folder="sent", mark_seen=False)
            finally:
                try:
                    mail.logout()
                except Exception:
                    pass
        except Exception as exc:
            logger.error("IMAP fetch_sent failed: %s", exc)
            return FetchResult(error=str(exc))

    def refetch_inbox_source(
        self, *, uid: int, uid_validity: int, email_id: str,
    ) -> FetchedEmail:
        """Refetch a durable source only while mailbox UID identity still matches.

        Bypasses the watermark and processed UID cache. A missing/moved message,
        UIDVALIDITY reset, or changed Message-ID requires manual reconciliation.
        """
        if uid <= 0 or uid_validity <= 0 or not email_id:
            raise ValueError("A positive UID, UIDVALIDITY and email ID are required")
        mail = self._connect()
        try:
            status, _ = mail.select(self.inbox_folder, readonly=True)
            if status != "OK":
                raise SourceUnavailable("IMAP inbox cannot be selected")
            if _read_uid_validity(mail) != uid_validity:
                raise SourceUnavailable("IMAP UIDVALIDITY changed or is unavailable")
            status, data = mail.uid("fetch", str(uid), "(RFC822)")
            if status != "OK" or not data or not isinstance(data[0], tuple):
                raise SourceUnavailable("IMAP source UID is missing")
            fetched = self._parse_message(data[0][1], uid=uid, folder="inbox")
            if fetched is None or fetched.email_id != email_id:
                raise SourceUnavailable("IMAP source identity no longer matches")
            return fetched
        finally:
            try:
                mail.logout()
            except Exception:
                pass

    def advance_watermark(self, uid: int, *, uid_validity: int | None = None) -> None:
        """Advance after durable capture without regressing this mailbox generation."""
        current, source, generation = self._watermark_state()
        if uid_validity is not None and source == self._source_identity() and generation == uid_validity:
            uid = max(uid, current)
        self._write_uid_watermark(uid, uid_validity=uid_validity)

    def health_check(self) -> bool:
        """Return True if IMAP login succeeds, False otherwise."""
        try:
            mail = self._connect()
            mail.logout()
            return True
        except Exception as exc:
            logger.warning("IMAP health_check failed: %s", exc)
            return False

    # ── Internal helpers ───────────────────────────────────────────

    def _connect(self):
        """Open and authenticate an IMAP connection. Caller must logout()."""
        if self.use_ssl:
            mail = imaplib.IMAP4_SSL(self.host, self.port)
        else:
            mail = imaplib.IMAP4(self.host, self.port)
        mail.login(self.username, self.password)
        return mail

    def _fetch_uids(
        self,
        conn: sqlite3.Connection,
        mail,
        uids: list[int],
        folder: str,
        mark_seen: bool,
    ) -> FetchResult:
        """Fetch, parse, and deduplicate a list of UIDs."""
        result = FetchResult()
        # Skip UIDs already known to be processed or unparseable
        cached_skip = 0
        for uid in uids:
            if uid in _processed_uids or uid in _failed_parse_uids:
                cached_skip += 1
                result.skipped_count += 1
                continue
            try:
                _, msg_data = mail.uid("fetch", str(uid), "(RFC822)")
                raw = msg_data[0][1]
                try:
                    fetched = self._parse_message(raw, uid=uid, folder=folder)
                except Exception as exc:
                    logger.warning("Failed to parse message UID %s: %s", uid, exc)
                    _cache_uid(uid, _failed_parse_uids)
                    continue
                if fetched is None:
                    _cache_uid(uid, _failed_parse_uids)
                    continue

                # Deduplication
                existing = conn.execute(
                    "SELECT email_id FROM processed_emails WHERE email_id = ?",
                    (fetched.email_id,),
                ).fetchone()
                if existing:
                    _cache_uid(uid, _processed_uids)
                    result.skipped_count += 1
                    continue

                if mark_seen:
                    mail.uid("store", str(uid), "+FLAGS", "\\Seen")

                result.fetched.append(fetched)
            except Exception as exc:
                # A network/IMAP/DB failure is not a malformed message. Do not
                # persist it as a permanently skipped UID or advance past it.
                logger.warning("Failed to fetch UID %s: %s", uid, exc)
                result.error = f"IMAP UID {uid} fetch failed; retry required"
        if cached_skip:
            logger.debug("Skipped %d cached UIDs (processed/failed)", cached_skip)
        return result

    def _parse_message(
        self, raw: bytes, uid: int, folder: str
    ) -> FetchedEmail | None:
        """
        Parse raw RFC822 bytes into a FetchedEmail.
        Extracts plain-text body; falls back to empty string for HTML-only messages.
        Generates a stable fallback email_id from From+Subject+Date if Message-ID absent.
        """
        try:
            msg = BytesParser(policy=policy.default).parsebytes(raw)

            # Message-ID with fallback
            msg_id = (msg.get("Message-ID") or "").strip()
            if not msg_id:
                fingerprint = (
                    (msg.get("From") or "")
                    + (msg.get("Subject") or "")
                    + (msg.get("Date") or "")
                )
                msg_id = "hash:" + hashlib.sha256(fingerprint.encode()).hexdigest()[:16]

            sender = (msg.get("From") or "").strip()
            # Extract bare address from "Name <addr>" format
            if "<" in sender and ">" in sender:
                sender = sender.split("<")[1].rstrip(">").strip()

            subject = (msg.get("Subject") or "").strip()
            in_reply_to = (msg.get("In-Reply-To") or "").strip() or None
            references = (msg.get("References") or "").strip() or None
            cc = (msg.get("Cc") or "").strip() or None
            bcc = (msg.get("Bcc") or "").strip() or None
            to_raw = (msg.get("To") or "").strip()
            from src.email.recipients import parse_recipient_list

            to_recipients = parse_recipient_list(to_raw, source="thread_participant")
            cc_recipients = parse_recipient_list(cc, source="thread_participant")
            bcc_recipients = parse_recipient_list(bcc, source="thread_participant")
            to_emails = [item["email"] for item in to_recipients]
            date_str = (msg.get("Date") or "").strip()

            # Parse received_at — fall back to now if unparseable
            sent_at = None
            try:
                from email.utils import parsedate_to_datetime
                authored = parsedate_to_datetime(date_str)
                if authored.tzinfo is not None:
                    sent_at = authored.astimezone(timezone.utc).isoformat()
            except (ValueError, TypeError, IndexError):
                pass
            try:
                from email.utils import parsedate_to_datetime
                dt = parsedate_to_datetime(date_str).astimezone(timezone.utc)
                received_at = dt.isoformat()
            except Exception:
                received_at = datetime.now(timezone.utc).isoformat()

            # Extract plain-text body
            body = _extract_plain_text(msg)

            # Extract all attachments (images + docs with metadata/bytes)
            atts = _extract_attachments(msg)
            from config.defaults import EMAIL_BODY_STORE_CHARS

            body = body.strip()
            coverage = inventory_source(
                raw, msg, email_id=msg_id, body=body, attachments=atts,
                stored_body_limit=EMAIL_BODY_STORE_CHARS,
            )

            return FetchedEmail(
                email_id=msg_id,
                sender_email=sender,
                subject=subject,
                body=body.strip(),
                received_at=received_at,
                sent_at=sent_at,
                folder=folder,
                in_reply_to=in_reply_to,
                references=references,
                cc=cc,
                bcc=bcc,
                to_emails=to_emails,
                to_recipients=to_recipients,
                cc_recipients=cc_recipients,
                bcc_recipients=bcc_recipients,
                uid=uid,
                attachments=atts,
                source_coverage=coverage,
                raw_source=raw,
            )
        except Exception as exc:
            logger.warning("Failed to parse message uid=%s: %s", uid, exc)
            return None
