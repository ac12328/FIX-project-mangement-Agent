"""Repository helpers for Wiki publication state and immutable receipts."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from src.wiki.publication.models import PublicationRecord


def publication_id_for(fact_id: str, fact_version: int) -> str:
    return "wpub_" + hashlib.sha256(f"{fact_id}|{fact_version}".encode()).hexdigest()[:24]


def get_publication(conn: sqlite3.Connection, publication_id: str) -> PublicationRecord | None:
    row = conn.execute("SELECT * FROM wiki_publications WHERE publication_id=?", (publication_id,)).fetchone()
    return PublicationRecord(**dict(row)) if row else None


def get_publication_for_fact(conn: sqlite3.Connection, fact_id: str) -> PublicationRecord | None:
    row = conn.execute("SELECT * FROM wiki_publications WHERE fact_id=?", (fact_id,)).fetchone()
    return PublicationRecord(**dict(row)) if row else None


def insert_plan(conn: sqlite3.Connection, *, plan) -> None:
    payload = plan.model_dump_json()
    conn.execute("""INSERT INTO wiki_publications
      (publication_id,fact_id,fact_version,decision_id,proposal_payload_json,proposed_at,
       canonical_path,operation,status,planned_content_hash,source_watermark,scope,visibility,
       project_ids_json,markets_json,counterparties_json,ownership_json)
      VALUES (?,?,?,?,?,?,?,?,'proposed',?,?,?,?,?,?,?,?)
      ON CONFLICT(fact_id) DO NOTHING""", (
        plan.publication_id, plan.fact_id, plan.fact_version, plan.decision_id, payload,
        plan.proposed_at, plan.canonical_path, plan.operation, plan.planned_content_hash,
        plan.source_watermark, plan.scope, plan.visibility,
        json.dumps(plan.project_ids), json.dumps(plan.markets),
        json.dumps(plan.counterparties), json.dumps(plan.ownership, sort_keys=True),
    ))


def set_status(conn: sqlite3.Connection, publication_id: str, status: str, *, error: str | None = None) -> None:
    conn.execute("UPDATE wiki_publications SET status=?,last_error=? WHERE publication_id=?", (status, error, publication_id))


def _aware_utc(now: datetime | None) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(timezone.utc)


def claim_publication(conn: sqlite3.Connection, publication_id: str, *,
                      lease_seconds: int = 300, now: datetime | None = None) -> str | None:
    claimed_at = _aware_utc(now)
    claimed_at_text = claimed_at.isoformat()
    previous = conn.execute("""SELECT status,lease_token,lease_expires_at,attempts
      FROM wiki_publications WHERE publication_id=?""", (publication_id,)).fetchone()
    if not previous:
        return None
    previous_status, previous_token, previous_expiry, _ = previous
    expired = (previous_status == "publishing" and previous_expiry is not None
               and previous_expiry <= claimed_at_text)
    if previous_status not in {"proposed", "failed"} and not expired:
        return None
    token = uuid.uuid4().hex
    lease_expires_at = (claimed_at + timedelta(seconds=lease_seconds)).isoformat()
    if expired:
        changed = conn.execute("""UPDATE wiki_publications SET status='publishing',lease_token=?,
          claimed_at=?,lease_expires_at=?,attempts=attempts+1,last_error=NULL
          WHERE publication_id=? AND status='publishing' AND lease_token IS ?
            AND lease_expires_at=? AND lease_expires_at<=?""", (
            token, claimed_at_text, lease_expires_at, publication_id,
            previous_token, previous_expiry, claimed_at_text,
        ))
    else:
        changed = conn.execute("""UPDATE wiki_publications SET status='publishing',lease_token=?,
          claimed_at=?,lease_expires_at=?,attempts=attempts+1,last_error=NULL
          WHERE publication_id=? AND status=?""", (
            token, claimed_at_text, lease_expires_at, publication_id, previous_status,
        ))
    if changed.rowcount != 1:
        return None
    if expired:
        payload = {"expired_token": previous_token, "lease_expires_at": previous_expiry}
        append_history(conn, publication_id=publication_id, event_type="lease_expired",
                       actor_id="wiki-publication-worker", reason="publication lease expired",
                       payload=payload)
        append_history(conn, publication_id=publication_id, event_type="reclaimed",
                       actor_id="wiki-publication-worker", reason="expired publication lease reclaimed",
                       payload={**payload, "lease_token": token})
    return token


def complete_claimed_status(publication_id: str, token: str, *, status: str,
                            error: str | None = None) -> bool:
    from src.db.connection import scoped_write
    with scoped_write() as conn:
        changed = conn.execute("""UPDATE wiki_publications SET status=?,last_error=?,
          lease_token=NULL,lease_expires_at=NULL WHERE publication_id=? AND status='publishing'
          AND lease_token=?""", (status, error, publication_id, token))
    return changed.rowcount == 1


def fail_publication(publication_id: str, token: str, *, error: str,
                     manual: bool = False, max_attempts: int = 3) -> str | None:
    """Fail only the matching live claim and record its terminal transition."""
    from src.db.connection import scoped_write
    with scoped_write() as conn:
        row = conn.execute("""SELECT attempts FROM wiki_publications
          WHERE publication_id=? AND status='publishing' AND lease_token=?""",
          (publication_id, token)).fetchone()
        if not row:
            return None
        status = "manual_review" if manual else (
            "dead_letter" if int(row[0] or 0) >= max_attempts else "failed"
        )
        changed = conn.execute("""UPDATE wiki_publications SET status=?,last_error=?,
          lease_token=NULL,lease_expires_at=NULL WHERE publication_id=? AND status='publishing'
          AND lease_token=?""", (status, error, publication_id, token))
        if changed.rowcount != 1:
            return None
        append_history(conn, publication_id=publication_id, event_type=status,
                       actor_id="wiki-publication-worker", reason="publication attempt failed",
                       payload={"error": error})
    return status


def set_revoke_phase(conn: sqlite3.Connection, *, publication_id: str, token: str,
                     phase: str, actor_id: str, payload: dict | None = None) -> None:
    changed = conn.execute("""UPDATE wiki_publications SET revoke_phase=?
      WHERE publication_id=? AND status='publishing' AND lease_token=?""",
      (phase, publication_id, token))
    if changed.rowcount != 1:
        raise RuntimeError("publication claim lost")
    append_history(conn, publication_id=publication_id, event_type=phase, actor_id=actor_id,
                   reason="durable revoke phase", payload=payload)


def insert_revision(conn: sqlite3.Connection, *, publication_id: str, revision: int,
                    repair_of_revision: int | None, content: str, content_hash: str,
                    page_hash: str, lint_issues: list[str]) -> None:
    rid = "wrev_" + hashlib.sha256(f"{publication_id}|{revision}|{content_hash}".encode()).hexdigest()[:24]
    conn.execute("""INSERT INTO wiki_publication_revisions
      (revision_id,publication_id,revision,repair_of_revision,content_hash,page_hash,content,lint_json)
      VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(publication_id,revision) DO NOTHING""",
      (rid, publication_id, revision, repair_of_revision, content_hash, page_hash,
       content, json.dumps(lint_issues, sort_keys=True)))


def append_history(conn: sqlite3.Connection, *, publication_id: str, event_type: str,
                   actor_id: str, reason: str, payload: dict | None = None) -> None:
    encoded = json.dumps(payload or {}, sort_keys=True)
    hid = "wph_" + hashlib.sha256(f"{publication_id}|{event_type}|{reason}|{encoded}".encode()).hexdigest()[:24]
    conn.execute("""INSERT INTO wiki_publication_history
      (history_id,publication_id,event_type,actor_id,reason,payload_json) VALUES (?,?,?,?,?,?)
      ON CONFLICT(history_id) DO NOTHING""", (hid, publication_id, event_type, actor_id, reason, encoded))


def insert_acknowledgement(conn: sqlite3.Connection, *, source_watermark: str,
                           publication_id: str, revision: int, content_hash: str,
                           page_hash: str, indexed_at: str) -> None:
    existing = conn.execute("SELECT * FROM wiki_publication_acknowledgements WHERE source_watermark=? OR publication_id=?",
                            (source_watermark, publication_id)).fetchall()
    for row in existing:
        if (row["source_watermark"], row["publication_id"], row["revision"], row["content_hash"], row["page_hash"]) != (
                source_watermark, publication_id, revision, content_hash, page_hash):
            raise ValueError("publication acknowledgement collision")
        return
    conn.execute("""INSERT INTO wiki_publication_acknowledgements
      (source_watermark,publication_id,revision,content_hash,page_hash,indexed_at)
      VALUES (?,?,?,?,?,?)""", (source_watermark, publication_id, revision, content_hash, page_hash, indexed_at))
    receipt = conn.execute("SELECT * FROM wiki_publication_acknowledgements WHERE source_watermark=?", (source_watermark,)).fetchone()
    if not receipt or receipt["publication_id"] != publication_id or receipt["page_hash"] != page_hash:
        raise ValueError("publication acknowledgement read-after-write mismatch")
