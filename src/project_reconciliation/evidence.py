"""Historical email loading and exact-citation validation."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from src.project_reconciliation.contracts import (
    EvidenceCitation,
    EvidenceRecord,
    FrozenEvidenceBundle,
)


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def freeze_email_bundle(
    conn: sqlite3.Connection, project_id: str,
) -> FrozenEvidenceBundle:
    """Load linked and subject-tagged historical email content into a frozen bundle."""
    tag = f"%[{project_id}]%"
    rows: list[dict] = []
    processed = conn.execute(
        """SELECT email_id AS source_id,
                  COALESCE(received_at, processed_at, '') AS event_at,
                  COALESCE(subject, '') AS subject,
                  COALESCE(sender_email, '') AS sender,
                  COALESCE(body_snippet, '') AS body,
                  CASE WHEN project_id=? THEN 'linked' ELSE 'subject_tag' END AS project_match
             FROM processed_emails
            WHERE project_id=? OR (project_id IS NULL AND subject LIKE ?)""",
        (project_id, project_id, tag),
    ).fetchall()
    for row in processed:
        item = dict(row)
        item["source_table"] = "processed_emails"
        rows.append(item)

    inbound = conn.execute(
        """SELECT reply_id AS source_id, received_at AS event_at,
                  COALESCE(subject, '') AS subject,
                  '' AS sender, COALESCE(body_text, '') AS body,
                  'linked' AS project_match
             FROM inbound_replies WHERE project_id=?""",
        (project_id,),
    ).fetchall()
    for row in inbound:
        item = dict(row)
        item["source_table"] = "inbound_replies"
        rows.append(item)

    records: list[EvidenceRecord] = []
    seen: set[tuple[str, str]] = set()
    for item in sorted(rows, key=lambda row: (str(row["event_at"]), str(row["source_id"]))):
        content = f"Subject: {item['subject']}\n{item['body']}".strip()
        content_hash = _hash_text(content)
        dedupe_key = (str(item["source_table"]), str(item["source_id"]))
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        source_ref = f"{item['source_table']}:{item['source_id']}"
        records.append(EvidenceRecord(
            source_ref=source_ref,
            source_table=str(item["source_table"]),
            source_id=str(item["source_id"]),
            project_match=str(item["project_match"]),
            event_at=str(item["event_at"] or ""),
            subject=str(item["subject"] or ""),
            sender=str(item["sender"] or ""),
            content=content,
            content_hash=content_hash,
        ))

    canonical = [record.model_dump(mode="json") for record in records]
    bundle_hash = _hash_text(json.dumps(canonical, sort_keys=True, ensure_ascii=False))
    return FrozenEvidenceBundle(
        project_id=project_id,
        records=records,
        bundle_hash=bundle_hash,
        frozen_at=datetime.now(timezone.utc).isoformat(),
    )


def validate_exact_citations(
    bundle: FrozenEvidenceBundle, citations: list[EvidenceCitation],
) -> None:
    """Fail closed if any quote is not present in its frozen source content."""
    contents = {record.source_ref: record.content for record in bundle.records}
    for citation in citations:
        content = contents.get(citation.source_ref)
        if content is None:
            raise ValueError(f"unknown citation source: {citation.source_ref}")
        if citation.quote not in content:
            raise ValueError(f"citation quote not found in {citation.source_ref}")
