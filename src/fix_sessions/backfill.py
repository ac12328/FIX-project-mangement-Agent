"""Historical inventory and conservative candidate backfill."""
from __future__ import annotations

import hashlib
import sqlite3
from collections import Counter
from typing import Any

from .extraction import extract_session_update
from .models import EmailAuthentication
from .service import ingest_configuration_update


def scan_historical_configuration_evidence(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Scan every project status. Historical records never gain trust implicitly."""
    sources: list[dict[str, Any]] = []
    emails = conn.execute(
        """SELECT email_id source_id, project_id, subject, body_snippet content,
                  sender_email, processed_at source_at, 'email' source_type
           FROM processed_emails WHERE project_id IS NOT NULL AND body_snippet IS NOT NULL"""
    ).fetchall()
    evidence = conn.execute(
        """SELECT evidence_id source_id, project_id, content_summary subject,
                  content_text content, NULL sender_email, evidence_at source_at,
                  evidence_type source_type
           FROM evidence_store WHERE content_text IS NOT NULL"""
    ).fetchall()
    for row in [*emails, *evidence]:
        item = dict(row)
        project_id = str(item.get("project_id") or "").strip()
        subject = str(item.get("subject") or "")
        if not project_id or project_id.startswith("_"):
            continue
        if item.get("source_type") == "manual_note" and subject.lower().startswith("historical wiki material migrated"):
            continue
        text = "\n".join((subject, item.get("content") or ""))
        lowered = text.lower()
        if "sendercompid" not in lowered or "targetcompid" not in lowered:
            continue
        try:
            update = extract_session_update(text)
            missing = []
            if not update.get("sender_comp_id"):
                missing.append("SenderCompID")
            if not update.get("target_comp_id"):
                missing.append("TargetCompID")
            if not update.get("endpoints"):
                missing.append("endpoint")
            if missing:
                item.update({
                    "status": "needs_review",
                    "reason": "incomplete FIX configuration: missing " + ", ".join(missing),
                    "environment": update["environment"],
                })
            else:
                item.update({"status": "extractable", "environment": update["environment"], "update": update})
        except ValueError as exc:
            item.update({"status": "needs_review", "reason": str(exc)})
        sources.append(item)
    return sources


def inventory_report(conn: sqlite3.Connection) -> dict[str, Any]:
    items = scan_historical_configuration_evidence(conn)
    projects = conn.execute("SELECT project_status, COUNT(*) count FROM projects GROUP BY project_status").fetchall()
    safe_items = []
    for item in items:
        safe_item = {
            key: item.get(key)
            for key in (
                "source_id", "project_id", "subject", "sender_email", "source_at",
                "source_type", "status", "reason", "environment",
            )
            if item.get(key) is not None
        }
        update = item.get("update") or {}
        if update:
            safe_item["extracted_fields"] = sorted(
                key for key, value in update.items()
                if key != "secrets" and value not in (None, "", [], {})
            )
            safe_item["endpoint_count"] = len(update.get("endpoints") or [])
            safe_item["has_username_secret"] = bool((update.get("secrets") or {}).get("username"))
            safe_item["has_password_secret"] = bool((update.get("secrets") or {}).get("password"))
        safe_items.append(safe_item)
    return {
        "project_counts": {row["project_status"]: row["count"] for row in projects},
        "source_counts": dict(Counter(item["source_type"] for item in items)),
        "status_counts": dict(Counter(item["status"] for item in items)),
        "items": safe_items,
    }


def create_pending_backfill_candidates(conn: sqlite3.Connection) -> dict[str, int]:
    """Create pending candidates only; old mail lacks trustworthy auth evidence."""
    counts = Counter()
    for item in scan_historical_configuration_evidence(conn):
        if item["status"] != "extractable":
            counts["needs_review"] += 1
            continue
        try:
            decision = ingest_configuration_update(
                conn, project_id=item["project_id"], environment=item["environment"],
                update=item["update"], sender_email=item.get("sender_email"),
                authentication=EmailAuthentication(),
                source_email_id=item["source_id"] if item["source_type"] == "email" else None,
                evidence_id=item["source_id"] if item["source_type"] != "email" else None,
                received_at=item.get("source_at"), auto_confirm=False,
                idempotency_key="fix-backfill:" + hashlib.sha256(f"{item['source_type']}:{item['source_id']}:{item['environment']}".encode()).hexdigest(),
            )
            counts[decision.status] += 1
        except ValueError:
            counts["incomplete"] += 1
    return dict(counts)
