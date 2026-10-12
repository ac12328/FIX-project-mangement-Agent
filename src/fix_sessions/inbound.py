"""Inbound-email bridge for canonical FIX session candidates."""
from __future__ import annotations

import hashlib
import logging
import sqlite3

from config import defaults
from .email_auth import parse_email_authentication
from .extraction import extract_session_update
from .secret_store import WindowsCredentialManagerStore
from .service import CandidateDecision, ingest_configuration_update

logger = logging.getLogger(__name__)


def process_inbound_fix_configuration(conn: sqlite3.Connection, *, message: dict, project_id: str | None) -> CandidateDecision | None:
    """Create a candidate when an already-linked email contains explicit FIX configuration."""
    if not defaults.FIX_SESSION_REGISTRY_ENABLED or not project_id:
        return None
    text = "\n".join((message.get("subject") or "", message.get("body") or ""))
    lowered = text.lower()
    if "sendercompid" not in lowered or "targetcompid" not in lowered or not any(token in lowered for token in ("host", "endpoint", " ip:")):
        return None
    try:
        update = extract_session_update(text)
        authentication = parse_email_authentication(message.get("authentication_headers") or {}, message.get("sender_email"))
        secret_store = WindowsCredentialManagerStore() if update.get("secrets") else None
        digest = hashlib.sha256((str(message.get("email_id")) + project_id + update["environment"]).encode("utf-8")).hexdigest()
        return ingest_configuration_update(
            conn, project_id=project_id, environment=update["environment"], update=update,
            sender_email=message.get("sender_email"), authentication=authentication,
            source_email_id=message.get("email_id"), received_at=message.get("received_at"),
            idempotency_key="fix-email:" + digest, secret_store=secret_store,
            auto_confirm=defaults.FIX_SESSION_AUTO_CONFIRM_ENABLED,
        )
    except (ValueError, RuntimeError) as exc:
        logger.info("FIX session email left for manual review: %s", exc)
        return None
