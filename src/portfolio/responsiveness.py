"""
Contact Responsiveness Scoring.

Computes responsiveness scores for contacts based on email response
patterns — outbound-to-inbound reply turnaround times.

Score tiers (median response hours):
  < 4h   → 1.0
  < 24h  → 0.75
  < 72h  → 0.5
  < 168h → 0.25
  >= 168h→ 0.1
"""

import json
import logging
import sqlite3
from datetime import datetime
from statistics import median

logger = logging.getLogger(__name__)


def score_from_median_hours(median_hours: float) -> float:
    """Convert a median response time (hours) to a 0-1 score."""
    if median_hours < 4:
        return 1.0
    if median_hours < 24:
        return 0.75
    if median_hours < 72:
        return 0.5
    if median_hours < 168:
        return 0.25
    return 0.1


def compute_responsiveness(
    conn: sqlite3.Connection, contact_id: str
) -> dict | None:
    """Compute responsiveness for a single contact.

    Looks up the contact's primary_email and email_addresses, then finds
    outbound->inbound email pairs via processed_emails (inbound.in_reply_to
    = outbound.email_id) where the inbound sender matches any of the
    contact's emails.

    Returns {"median_hours": float, "score": float, "sample_size": int}
    or None if no reply pairs are found.
    """
    row = conn.execute(
        "SELECT primary_email, email_addresses FROM contacts WHERE contact_id = ?",
        (contact_id,),
    ).fetchone()
    if not row:
        return None

    emails: set[str] = set()
    primary = row["primary_email"]
    if primary:
        emails.add(primary.lower().strip())

    raw_addrs = row["email_addresses"] or "[]"
    try:
        addrs = json.loads(raw_addrs) if isinstance(raw_addrs, str) else raw_addrs
    except (json.JSONDecodeError, TypeError):
        addrs = []
    if isinstance(addrs, list):
        for addr in addrs:
            if isinstance(addr, str) and addr.strip():
                emails.add(addr.lower().strip())

    if not emails:
        return None

    # Build placeholders for the IN clause
    placeholders = ",".join("?" for _ in emails)
    email_list = list(emails)

    # Find outbound→inbound reply pairs where the inbound sender is the contact
    query = f"""
        SELECT
            outbound.processed_at AS sent_at,
            inbound.processed_at AS reply_at
        FROM processed_emails AS inbound
        JOIN processed_emails AS outbound
            ON inbound.in_reply_to = outbound.email_id
        WHERE outbound.direction = 'outbound'
          AND inbound.direction = 'inbound'
          AND LOWER(inbound.sender_email) IN ({placeholders})
          AND outbound.processed_at IS NOT NULL
          AND inbound.processed_at IS NOT NULL
    """
    rows = conn.execute(query, email_list).fetchall()

    if not rows:
        return None

    deltas_hours: list[float] = []
    for r in rows:
        try:
            sent = _parse_dt(r["sent_at"])
            replied = _parse_dt(r["reply_at"])
            delta = (replied - sent).total_seconds() / 3600.0
            if delta >= 0:
                deltas_hours.append(delta)
        except (ValueError, TypeError):
            continue

    if not deltas_hours:
        return None

    med = median(deltas_hours)
    return {
        "median_hours": round(med, 2),
        "score": score_from_median_hours(med),
        "sample_size": len(deltas_hours),
    }


def update_all_responsiveness_scores(conn: sqlite3.Connection) -> int:
    """Recompute and persist responsiveness_score for all person contacts.

    Returns the number of contacts updated.
    """
    rows = conn.execute(
        "SELECT contact_id FROM contacts WHERE contact_type = 'person'"
    ).fetchall()

    updated = 0
    for row in rows:
        cid = row["contact_id"]
        result = compute_responsiveness(conn, cid)
        if result is not None:
            conn.execute(
                "UPDATE contacts SET responsiveness_score = ? WHERE contact_id = ?",
                (result["score"], cid),
            )
            updated += 1

    conn.commit()
    logger.info("Updated responsiveness scores for %d contacts", updated)
    return updated


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_dt(value: str) -> datetime:
    """Parse an ISO-8601 datetime string (with or without timezone)."""
    # Try with timezone first, then without
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise ValueError(f"Cannot parse datetime: {value}")
