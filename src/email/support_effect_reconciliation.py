"""Read-only inventory of unresolved support-effect route provenance.

Only pending card or pending/scheduled draft intents are actionable here. Unknown
provenance and inconsistent known-source receipts require manual reconciliation;
listing them never releases a card or draft.
"""

from __future__ import annotations

import sqlite3


def list_unknown_support_effects(conn: sqlite3.Connection) -> list[dict[str, object]]:
    """Return actionable unknown-source intents without claiming or modifying them."""
    cursor = conn.execute(
        """SELECT effect.email_id, effect.ticket_id, effect.route_source_type,
                  effect.card_state, effect.draft_state, effect.available_at,
                  effect.draft_available_at, effect.created_at,
                  route.source_type AS inbound_source_type,
                  route.route_state AS inbound_route_state
           FROM support_email_effects AS effect
           LEFT JOIN inbound_email_work AS route ON route.email_id = effect.email_id
           WHERE effect.route_source_type = 'unknown'
             AND (effect.card_state = 'pending'
                  OR effect.draft_state IN ('pending','scheduled'))
           ORDER BY effect.created_at, effect.email_id"""
    )
    return [dict(zip((column[0] for column in cursor.description), row))
            for row in cursor.fetchall()]


def list_known_source_support_effects_needing_reconciliation(
    conn: sqlite3.Connection,
) -> list[dict[str, object]]:
    """Report actionable IMAP/Graph effects with missing or inconsistent receipts.

    A matching pending/routing receipt is still blocked by the worker, but is not
    an inventory defect. A source mismatch takes precedence over route state.
    """
    cursor = conn.execute(
        """SELECT effect.email_id, effect.ticket_id, effect.route_source_type,
                  effect.card_state, effect.draft_state, effect.available_at,
                  effect.draft_available_at, effect.created_at,
                  route.source_type AS inbound_source_type,
                  route.route_state AS inbound_route_state,
                  CASE
                    WHEN route.email_id IS NULL THEN 'missing_route'
                    WHEN route.source_type != effect.route_source_type
                      THEN 'source_mismatch'
                    ELSE 'needs_reconciliation'
                  END AS reconciliation_reason
           FROM support_email_effects AS effect
           LEFT JOIN inbound_email_work AS route ON route.email_id = effect.email_id
           WHERE effect.route_source_type IN ('imap','graph')
             AND (effect.card_state = 'pending'
                  OR effect.draft_state IN ('pending','scheduled'))
             AND (route.email_id IS NULL
                  OR route.source_type != effect.route_source_type
                  OR route.route_state = 'needs_reconciliation')
           ORDER BY effect.created_at, effect.email_id"""
    )
    return [dict(zip((column[0] for column in cursor.description), row))
            for row in cursor.fetchall()]
