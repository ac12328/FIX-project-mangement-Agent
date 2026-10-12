"""
Microsoft Graph inbox poller.

Fetches new messages, deduplicates via processed_emails,
runs 3-stage triage, and routes classified events:
  - operational  → handle_operational_email (silent)
  - project_update with known project → agent_loop() via inbound_policy (silent)
  - new_inquiry or requires_operator_triage → Telegram notification card

Returns a list of notification dicts for the async scheduler job to dispatch.
Notification dict: {profile, title, body, project_id}
"""

import hashlib
import json
import logging
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone

from requests.exceptions import HTTPError

from config.defaults import EMAIL_BODY_STORE_CHARS
from config.settings import CONFIG, DATA_DIR
from src.email.graph_client import GraphClient
from src.email.graph_control import begin_graph_control, dedicated_graph_poll
from src.email.inbox_fetcher import FetchedEmail
from src.email.inbound_work import (
    InboundRouteClaim, RouteClaimExpectation, checkpoint_route_step, claim_pending_route,
    expire_routing_leases, finish_route, list_pending_graph, mark_pending_source_retry,
    require_reconciliation,
)
from src.email.email_processor import publish_project_email_processed
from src.email.inbound_policy import build_email_classifier, route_inbound_event
from src.email.recipients import serialize_recipient_header
from src.triage.email_triage import ClassifiedEvent, triage_email

logger = logging.getLogger(__name__)
GRAPH_RECOVERY_BATCH_LIMIT = 500


@dedicated_graph_poll
def poll_graph_inbox(conn: sqlite3.Connection) -> list[dict]:
    """
    Main polling entry point. Reads new messages from the configured Graph folder.

    Args:
        conn: Idle dedicated SQLite connection (WAL mode, foreign_keys=ON).
            Do not pass a connection already inside scoped_write() or a caller
            transaction: this poller commits source claims and route receipts.

    Returns:
        List of notification dicts for events requiring operator attention.
    """
    cfg = CONFIG.msgraph
    client = GraphClient(
        tenant_id=cfg.tenant_id,
        client_id=cfg.client_id,
        client_secret=cfg.client_secret,
        user_email=cfg.user_email,
    )

    notifications: list[dict] = []
    account_key = hashlib.sha256(
        f"{cfg.tenant_id.lower()}:{cfg.user_email.lower()}".encode("utf-8")
    ).hexdigest()

    # Discovery cursors are not route receipts. Recover never-started work first,
    # even when the cursor has advanced or the source has moved folders.
    expired_due = conn.execute("""SELECT 1 FROM inbound_email_work
        WHERE source_type='graph' AND route_state='routing'
          AND lease_expires_at <= datetime('now') LIMIT 1""").fetchone()
    if expired_due:
        expired = expire_routing_leases(conn, source_type="graph")
        conn.commit()
        logger.error("Graph poll: %d expired route leases need reconciliation", expired)
    pending = list_pending_graph(
        conn, account_key, cfg.poll_folder, limit=GRAPH_RECOVERY_BATCH_LIMIT,
    )
    for row in pending:
        email_id = row["email_id"]
        try:
            checkpoint = json.loads(row["checkpoint_json"] or "{}")
            graph_id = checkpoint.get("graph_id")
            if not isinstance(graph_id, str) or not graph_id:
                raise ValueError("missing captured Graph ID")
            event = ClassifiedEvent(**json.loads(row["classification_json"]))
            anchor = conn.execute(
                "SELECT sender_email, subject, result, project_id "
                "FROM processed_emails WHERE email_id=?",
                (email_id,),
            ).fetchone()
            if anchor is None or event.email_id != email_id or (
                event.sender_email != anchor["sender_email"] or
                event.subject != anchor["subject"] or
                event.classification_type != anchor["result"] or
                event.project_id != anchor["project_id"]
            ):
                raise ValueError("classification snapshot differs from captured anchor")
        except (ValueError, TypeError, AttributeError) as exc:
            logger.exception("Graph pending classification needs reconciliation for %s", email_id)
            _reconcile_pending_graph(conn, email_id, reason=(
                f"Graph recovery classification invalid: {type(exc).__name__}"
            ))
            continue
        try:
            msg = client.fetch_message_by_id(graph_id)
        except HTTPError as exc:
            response = exc.response
            if response is not None and response.status_code in {400, 404, 410}:
                logger.exception("Graph pending source unavailable for %s", email_id)
                _reconcile_pending_graph(conn, email_id, reason=(
                    f"Graph recovery source unavailable: HTTP {response.status_code}"
                ))
            else:
                _retry_pending_graph_source(conn, email_id)
                logger.warning("Graph recovery will retry source %s next poll", email_id,
                               exc_info=True)
            continue
        except Exception:
            _retry_pending_graph_source(conn, email_id)
            logger.warning("Graph recovery will retry source %s next poll", email_id,
                           exc_info=True)
            continue
        if not isinstance(msg, dict) or not msg or (
            msg.get("graph_id") != graph_id or msg.get("email_id") != email_id or
            msg.get("sender_email") != anchor["sender_email"] or
            msg.get("subject") != anchor["subject"]
        ):
            logger.error("Graph pending source identity differs for %s", email_id)
            _reconcile_pending_graph(conn, email_id,
                                     reason="Graph recovery source differs from captured identity")
            continue
        begin_graph_control(conn)
        route_token = claim_pending_route(
            conn, email_id, RouteClaimExpectation.from_pending(row, event),
        )
        conn.commit()  # Claim before any FIX candidate or policy route effect.
        if route_token is None:
            _reconcile_pending_graph(conn, email_id, reason="Graph claim snapshot changed")
            continue
        _route_claimed_graph(conn, msg, event, route_token, cfg.poll_folder, notifications)

    since = client.load_last_poll_time(str(DATA_DIR), cfg.poll_folder)
    messages = client.fetch_new_messages(folder=cfg.poll_folder, since_datetime=since)
    if not messages:
        logger.debug("Graph poll: no new messages in %s", cfg.poll_folder)
        if cfg.poll_sent_items:
            _sync_sent_items(conn, client, str(DATA_DIR))
        return notifications

    classifier = build_email_classifier()
    now = datetime.now(timezone.utc).isoformat()

    for msg in messages:
        email_id = msg["email_id"]

        # Deduplicate — skip already-processed messages
        existing = conn.execute(
            "SELECT email_id FROM processed_emails WHERE email_id = ?",
            (email_id,),
        ).fetchone()
        if existing:
            continue

        # 3-stage triage: deterministic → heuristic → LLM
        event = triage_email(
            conn,
            email_id=email_id,
            sender_email=msg["sender_email"],
            subject=msg["subject"],
            body=msg["body"],
            thread_id=msg.get("thread_id"),
            in_reply_to=msg.get("in_reply_to"),
            llm_classify_fn=classifier,
        )

        if (event.email_id != email_id or
            event.sender_email != msg["sender_email"] or
            event.subject != msg["subject"]
        ):
            raise ValueError("Graph classification does not match its source")

        # Record in processed_emails as deduplication anchor
        thread_fp = (msg.get("in_reply_to") or "").strip("<>") or email_id
        begin_graph_control(conn)
        conn.execute("SAVEPOINT graph_capture")
        try:
            cur = conn.execute(
                """INSERT OR IGNORE INTO processed_emails
               (email_id, project_id, processed_at, received_at, result, thread_fp, sender_email, subject,
                direction, thread_id, in_reply_to, references_chain, cc, body_snippet,
                to_recipients_json, cc_recipients_json, bcc_recipients_json, sent_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (email_id, event.project_id, now, msg.get("received_at") or now,
             event.classification_type, thread_fp, msg["sender_email"], msg["subject"],
             "inbound", msg.get("thread_id") or thread_fp, msg.get("in_reply_to"),
             msg.get("references"), msg.get("cc"), (msg.get("body") or "")[:EMAIL_BODY_STORE_CHARS],
             serialize_recipient_header(msg.get("to_recipients"), msg.get("to_emails")),
             serialize_recipient_header(msg.get("cc_recipients"), msg.get("cc")),
             serialize_recipient_header(msg.get("bcc_recipients"), msg.get("bcc")), msg.get("sent_at")),
            )
            if cur.rowcount:
                classification_json = json.dumps(asdict(event), ensure_ascii=False)
                checkpoint_json = json.dumps({"graph_id": msg.get("graph_id")})
                conn.execute("""INSERT INTO inbound_email_work
                    (email_id, source_type, account_key, mailbox, classification_json,
                     checkpoint_json, route_state)
                    VALUES (?, 'graph', ?, ?, ?, ?, 'pending')""",
                    (email_id, account_key, cfg.poll_folder,
                     classification_json, checkpoint_json))
            elif conn.execute(
                "SELECT 1 FROM inbound_email_work WHERE email_id=?", (email_id,),
            ).fetchone() is None:
                conn.execute("""INSERT INTO inbound_email_work
                    (email_id, source_type, classification_json, route_state, last_error)
                    VALUES (?, 'legacy', '{}', 'needs_reconciliation',
                            'pre-existing anchor without a route receipt')""", (email_id,))
            conn.execute("RELEASE SAVEPOINT graph_capture")
        except BaseException:
            conn.execute("ROLLBACK TO SAVEPOINT graph_capture")
            conn.execute("RELEASE SAVEPOINT graph_capture")
            conn.rollback()  # Dedicated capture owns the outer transaction.
            raise

        # Commit the source capture before starting any effects. This does not
        # release a caller-owned serializer lease.
        conn.commit()
        if cur.rowcount == 0:
            continue
        expected = RouteClaimExpectation(
            source_type="graph", account_key=account_key, mailbox=cfg.poll_folder,
            uid_validity=None, source_uid=None,
            classification_json=classification_json, checkpoint_json=checkpoint_json,
            event=event,
        )
        begin_graph_control(conn)
        route_token = claim_pending_route(conn, email_id, expected)
        conn.commit()  # Durable claim before FIX candidate or route side effects.
        if route_token is None:
            _reconcile_pending_graph(conn, email_id, reason="fresh Graph claim snapshot changed")
            continue
        if not msg.get("graph_id"):
            _reconcile_graph_claim(conn, email_id, route_token,
                                   reason="Graph capture missing message ID")
            continue
        _route_claimed_graph(conn, msg, event, route_token, cfg.poll_folder, notifications)

    # Advance poll cursor to the most recent message's received_at
    latest_ts = messages[0].get("received_at")
    pending = conn.execute("""SELECT 1 FROM inbound_email_work
        WHERE source_type='graph' AND account_key=? AND mailbox=? AND route_state='pending'
        LIMIT 1""", (account_key, cfg.poll_folder)).fetchone()
    if latest_ts and not pending:
        client.save_last_poll_time(str(DATA_DIR), cfg.poll_folder, latest_ts)

    logger.info(
        "Graph poll: processed %d message(s), %d notification(s)",
        len(messages),
        len(notifications),
    )

    # Optionally sync Sent Items folder (for manually-sent Outlook emails)
    if cfg.poll_sent_items:
        _sync_sent_items(conn, client, str(DATA_DIR))

    return notifications


def _retry_pending_graph_source(conn: sqlite3.Connection, email_id: str) -> None:
    """A failed read has no route effects; rotate the durable pending source."""
    mark_pending_source_retry(conn, email_id, source_type="graph")
    conn.commit()


def _reconcile_pending_graph(conn: sqlite3.Connection, email_id: str, *, reason: str) -> None:
    """Quarantine invalid identity without ever routing it."""
    begin_graph_control(conn)
    token = claim_pending_route(conn, email_id)
    conn.commit()
    if token is not None:
        _reconcile_graph_claim(conn, email_id, token, reason=reason)


def _reconcile_graph_claim(conn: sqlite3.Connection, email_id: str, token: str,
                           *, reason: str) -> None:
    begin_graph_control(conn)
    if not require_reconciliation(conn, email_id, token, reason=reason):
        conn.rollback()
        raise RuntimeError("Graph route claim was lost before reconciliation")
    conn.commit()


def _route_claimed_graph(conn: sqlite3.Connection, msg: dict, event: ClassifiedEvent,
                         route_token: str, folder: str, notifications: list[dict]) -> None:
    """One route state machine for new and recovered, durably claimed Graph work."""
    email_id = event.email_id
    # Extract explicit FIX configuration before any routing LLM sees the body.
    # This bridge can commit independently, so a crash must leave the route
    # claimed (and eventually needing reconciliation), never safely pending.
    fix_candidate_failed = False
    try:
        begin_graph_control(conn)
        if not checkpoint_route_step(
            conn, email_id, route_token, step="fix_candidate", status="started",
        ):
            raise RuntimeError("Graph FIX candidate claim lost before start")
        conn.commit()
        try:
            from src.fix_sessions.inbound import process_inbound_fix_configuration
            process_inbound_fix_configuration(conn, message=msg, project_id=event.project_id)
        except Exception:
            fix_candidate_failed = True
            conn.rollback()  # The bridge may already have committed other effects.
            logger.exception("FIX session candidate ingestion failed for %s", email_id)
        begin_graph_control(conn)
        if not checkpoint_route_step(
            conn, email_id, route_token, step="fix_candidate",
            status="incomplete" if fix_candidate_failed else "completed",
        ):
            raise RuntimeError("Graph FIX candidate claim lost before receipt")
        conn.commit()
    except BaseException:
        conn.rollback()
        try:
            _reconcile_graph_claim(
                conn, email_id, route_token,
                reason="FIX candidate interrupted; committed effects unknown",
            )
        except Exception:
            conn.rollback()
            logger.exception("Graph FIX candidate reconciliation failed for %s", email_id)
        raise

    # Route based on classification result (may call LLM)
    fetched = FetchedEmail(
        email_id=email_id,
        sender_email=msg.get("sender_email") or "",
        subject=msg.get("subject") or "",
        body=msg.get("body") or "",
        received_at=msg.get("received_at") or datetime.now(timezone.utc).isoformat(),
        sent_at=msg.get("sent_at"),
        folder=folder,
        in_reply_to=msg.get("in_reply_to"),
        references=msg.get("references"),
        cc=msg.get("cc"),
        bcc=msg.get("bcc"),
        to_emails=list(msg.get("to_emails") or []),
        to_recipients=list(msg.get("to_recipients") or []),
        cc_recipients=list(msg.get("cc_recipients") or []),
        bcc_recipients=list(msg.get("bcc_recipients") or []),
    )
    post_commit = []
    try:
        # Make the command start durable before policy may commit effects.
        # An interrupted or uncertain route is never safe to replay.
        begin_graph_control(conn)
        if not checkpoint_route_step(
            conn, email_id, route_token, step="policy_route", status="started",
        ):
            raise RuntimeError("Graph policy route claim lost before start")
        conn.commit()
        # Route analysis may read for a long time or commit through a separate
        # short scope. Leave this connection idle until policy first writes.
        notif = route_inbound_event(
            conn, event, msg["body"], fetched=fetched, post_commit=post_commit,
            route_claim=InboundRouteClaim(email_id=email_id, token=route_token),
        )
        policy_incomplete = bool(notif and notif.get("route_requires_reconciliation"))
        # A route with no writes still needs one receipt transaction. Routes
        # with writes keep this same transaction through their final receipt.
        if not conn.in_transaction:
            conn.execute("BEGIN")
        if not checkpoint_route_step(
            conn, email_id, route_token, step="policy_route",
            status="incomplete" if policy_incomplete else "completed",
        ):
            raise RuntimeError("Graph policy route claim lost before receipt")
        needs_reconciliation = fix_candidate_failed or policy_incomplete
        if needs_reconciliation:
            from src.email.inbound_presentation_outbox import block_presentation_for_reconciliation

            # The policy can have prepared a success card before the independent
            # FIX candidate failure is known to the final route receipt. Keep
            # its intent for inspection, but never let the worker send it.
            block_presentation_for_reconciliation(conn, email_id)
        if needs_reconciliation:
            updated = require_reconciliation(
                conn, email_id, route_token,
                reason=("FIX candidate ingestion failed; committed effects unknown"
                        if fix_candidate_failed else
                        "route reported incomplete application; effects unknown"),
            )
        else:
            # A Graph support digest must not outlive a failed/uncertain route.
            from src.email.support_effects import release_support_route_digests

            release_support_route_digests(
                conn, email_id, route_token, source_type="graph",
            )
            updated = finish_route(conn, email_id, route_token)
        if not updated:
            raise RuntimeError("Graph route claim was lost before its receipt")
        # A returned failure is a reconciliation receipt, not a completed route.
        conn.commit()
    except BaseException:
        conn.rollback()
        try:
            begin_graph_control(conn)
            require_reconciliation(conn, email_id, route_token,
                                   reason="route exception; committed effects unknown")
            conn.commit()
        except Exception:
            conn.rollback()
            logger.exception("Graph route reconciliation receipt failed for %s", email_id)
        raise
    if fix_candidate_failed:
        if notif and notif.get("route_requires_reconciliation"):
            notifications.append(notif)
        elif event.suppressed_reason != "delivery_failure":
            notifications.append({
                "profile": "B", "title": "Graph email needs reconciliation",
                "body": (f"FIX candidate ingestion failed for {email_id}. Verify any "
                         "committed effects before reconciling; do not replay this email."),
                "project_id": event.project_id,
                "route_requires_reconciliation": True,
            })
    elif notif:
        notifications.append(notif)
    if needs_reconciliation:
        # Preserve only failure notices; never publish success or replay.
        return
    if post_commit:
        from src.db.connection import run_external_io_with_receipt_scope

        def _deliver_committed_cards(_receipt_conn):
            for effect in post_commit:
                try:
                    effect()
                except Exception:
                    logger.exception("Post-commit card dispatch failed for %s", email_id)

        # A serializer-owned caller must relinquish its lease during delivery.
        run_external_io_with_receipt_scope(conn, _deliver_committed_cards)
    if event.ticket_id:
        from src.email.support_effects import process_support_effects

        try:
            process_support_effects(email_id=email_id)
        except Exception:
            logger.exception("Committed support effects deferred for %s", email_id)
    publish_project_email_processed(fetched, event)


def _sync_sent_items(
    conn: sqlite3.Connection,
    client,
    data_dir: str,
) -> None:
    """
    Passively sync the Sent Items folder.

    Records outbound message-ids into processed_emails so that future
    replies from clients can be matched back to their project via
    threading.find_thread_project(). Does NOT run triage on sent emails.
    """
    since = client.load_last_poll_time(data_dir, "sentitems")
    sent = client.fetch_new_messages(folder="sentitems", since_datetime=since)
    if not sent:
        return

    now = datetime.now(timezone.utc).isoformat()
    for msg in sent:
        conn.execute(
            """INSERT OR IGNORE INTO processed_emails
               (email_id, project_id, processed_at, received_at, result, sender_email, subject, direction,
                thread_id, in_reply_to, references_chain, cc, body_snippet,
                to_recipients_json, cc_recipients_json, bcc_recipients_json, sent_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (msg["email_id"], None, now, msg.get("received_at") or now, "sent_outbound",
             msg.get("sender_email"), msg.get("subject"), "outbound",
             msg.get("thread_id"), msg.get("in_reply_to"), msg.get("references"), msg.get("cc"),
             (msg.get("body") or "")[:EMAIL_BODY_STORE_CHARS],
             serialize_recipient_header(msg.get("to_recipients"), msg.get("to_emails")),
             serialize_recipient_header(msg.get("cc_recipients"), msg.get("cc")),
             serialize_recipient_header(msg.get("bcc_recipients"), msg.get("bcc"))),
        )
    conn.commit()

    latest_ts = sent[0].get("received_at")
    if latest_ts:
        client.save_last_poll_time(data_dir, "sentitems", latest_ts)

    logger.info("Graph sent-items sync: recorded %d outbound email(s)", len(sent))
