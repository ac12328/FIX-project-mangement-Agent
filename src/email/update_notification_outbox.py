"""Durable delivery of committed update cards, independent from project writes.

Only proven pre-send refusals are retryable. Expired in-flight deliveries become
uncertain: automatic retry cannot guarantee no duplicates after remote acceptance.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import hashlib
import logging
from uuid import uuid4
from src.email.update_outcomes import (
    AUTO_APPLIED_SENT, AUTO_APPLIED_NOTIFICATION_PENDING, AUTO_APPLIED_NOTIFICATION_UNKNOWN,
    committed_notification_outcome,
)
logger = logging.getLogger(__name__)


def _now(at=None):
    value=at or datetime.now(timezone.utc)
    if value.tzinfo is None: raise ValueError("timezone-aware time required")
    return value.astimezone(timezone.utc)


@contextmanager
def _db(conn=None):
    if conn is not None:
        yield conn
    else:
        from src.db.connection import scoped_write
        with scoped_write() as scoped: yield scoped


def _configured_destination():
    from config.settings import CONFIG
    telegram = CONFIG.telegram
    fallback = list(telegram.allowed_chat_ids or [])
    chat_id = telegram.chat_id or (fallback[0] if fallback else 0)
    token = str(telegram.bot_token or "")
    return {
        "chat_id": int(chat_id) if chat_id else None,
        "bot_fingerprint": hashlib.sha256(token.encode()).hexdigest() if token else None,
    }


def _bind_payload(payload):
    destination = _configured_destination()
    return {**payload, "chat_id": destination["chat_id"], "_delivery_binding": destination}


def _destination_error(payload):
    bound = payload.get("_delivery_binding")
    if not isinstance(bound, dict) or not bound.get("chat_id") or not bound.get("bot_fingerprint"):
        return "recipient_unbound"
    current = _configured_destination()
    if payload.get("chat_id") != bound["chat_id"] or current["chat_id"] != bound["chat_id"]:
        return "recipient_changed"
    if current["bot_fingerprint"] != bound["bot_fingerprint"]:
        return "bot_identity_changed"
    return None


def enqueue_update_notification(conn, *, update_id, project_id, payload):
    """No commit: same transaction as project write, accepted receipt and undo."""
    row=conn.execute("SELECT project_id,decision_outcome FROM proposed_updates WHERE update_id=?",(update_id,)).fetchone()
    kind = payload.get("_notification_kind", "update_undo")
    if kind not in {"update_undo", "pct_completed", "review_card"}:
        raise ValueError("unsupported notification kind")
    required_decision = "needs_review" if kind == "review_card" else "accepted"
    if not row or row[0] != project_id or row[1] != required_decision:
        raise ValueError(f"{required_decision} project update required")
    if payload.get("update_id")!=update_id or payload.get("project_id")!=project_id: raise ValueError("notification scope mismatch")
    payload = _bind_payload(payload)
    encoded=json.dumps(payload,ensure_ascii=False,sort_keys=True)
    prior=conn.execute("SELECT payload_json FROM email_update_notification_outbox WHERE update_id=?",(update_id,)).fetchone()
    if prior:
        if prior[0]!=encoded: raise ValueError("notification identity collision")
        return
    now=_now().isoformat()
    conn.execute("INSERT INTO email_update_notification_outbox (update_id,project_id,payload_json,available_at,created_at,updated_at) VALUES (?,?,?,?,?,?)",(update_id,project_id,encoded,now,now,now))


def recover_expired_notifications(conn, *, at=None):
    now=_now(at).isoformat()
    return conn.execute("UPDATE email_update_notification_outbox SET state='uncertain',last_error='delivery_lease_expired',updated_at=? WHERE state='sending' AND lease_until<=?",(now,now)).rowcount


def _delivery_outcome(kind, outcome):
    """Keep review-card outcomes distinct from accepted project updates."""
    if kind != "review_card":
        return outcome
    return {
        AUTO_APPLIED_SENT: "review_card",
        AUTO_APPLIED_NOTIFICATION_PENDING: "review_notification_pending",
        AUTO_APPLIED_NOTIFICATION_UNKNOWN: "review_notification_unknown",
    }[outcome]


def deliver_update_notification(update_id, *, conn=None, sender=None, at=None):
    """Claim durably, send, then record result. Never write project fields."""
    now=_now(at);token=uuid4().hex
    with _db(conn) as db:
        recover_expired_notifications(db,at=now)
        row=db.execute("SELECT o.*,p.decision_outcome FROM email_update_notification_outbox o JOIN proposed_updates p USING(update_id) WHERE update_id=?",(update_id,)).fetchone()
        if not row:
            db.commit();return AUTO_APPLIED_NOTIFICATION_UNKNOWN
        row=dict(row)
        kind = json.loads(row["payload_json"]).get("_notification_kind", "update_undo")
        if row["state"]=="sent": db.commit();return _delivery_outcome(kind, AUTO_APPLIED_SENT)
        required_decision = "needs_review" if kind == "review_card" else "accepted"
        if row["decision_outcome"] != required_decision:
            db.execute("UPDATE email_update_notification_outbox SET state='cancelled',updated_at=? WHERE update_id=? AND state='pending'",(now.isoformat(),update_id))
            db.commit();return _delivery_outcome(kind, AUTO_APPLIED_NOTIFICATION_UNKNOWN)
        if row["state"] in {"sending","uncertain","cancelled"}:
            db.commit();return _delivery_outcome(kind, AUTO_APPLIED_NOTIFICATION_UNKNOWN)
        payload = json.loads(row["payload_json"])
        binding_error = _destination_error(payload)
        if binding_error:
            db.execute("UPDATE email_update_notification_outbox SET last_error=?,available_at=?,updated_at=? WHERE update_id=? AND state='pending'",
                (binding_error,(now+timedelta(minutes=5)).isoformat(),now.isoformat(),update_id))
            db.commit()
            return _delivery_outcome(kind, AUTO_APPLIED_NOTIFICATION_PENDING)
        changed=db.execute("UPDATE email_update_notification_outbox SET state='sending',lease_token=?,lease_until=?,attempts=attempts+1,updated_at=? WHERE update_id=? AND state='pending' AND available_at<=?",(token,(now+timedelta(minutes=5)).isoformat(),now.isoformat(),update_id,now.isoformat()))
        db.commit()
        if changed.rowcount!=1: return _delivery_outcome(kind, AUTO_APPLIED_NOTIFICATION_PENDING)
    try:
        if sender is None:
            if payload.get("_notification_kind") == "pct_completed":
                from src.telegram.pct_notice import push_pct_completed_notice_sync
                sender=push_pct_completed_notice_sync
            elif payload.get("_notification_kind") == "review_card":
                from src.telegram.review_push import push_review_card_sync
                sender=push_review_card_sync
            else:
                from src.telegram.review_push import push_auto_applied_undo_card_sync
                sender=push_auto_applied_undo_card_sync
        send_payload = {key: value for key, value in payload.items() if key not in {"_delivery_binding", "_notification_kind"}}
        send_payload["expected_bot_fingerprint"] = payload["_delivery_binding"]["bot_fingerprint"]
        result=sender(conn,**send_payload)
        outcome=committed_notification_outcome(result)
    except Exception:
        logger.exception("Delivery uncertain for committed update %s",update_id)
        result={};outcome=AUTO_APPLIED_NOTIFICATION_UNKNOWN
    state={AUTO_APPLIED_SENT:"sent",AUTO_APPLIED_NOTIFICATION_PENDING:"pending"}.get(outcome,"uncertain")
    message_id=str(result["telegram_message_id"]) if state=="sent" else None
    error=None if state=="sent" else "pre_send_refusal" if state=="pending" else "delivery_unconfirmed"
    try:
        with _db(conn) as db:
            changed=db.execute("UPDATE email_update_notification_outbox SET state=?,available_at=?,telegram_message_id=?,last_error=?,lease_until=NULL,updated_at=? WHERE update_id=? AND state='sending' AND lease_token=?",(state,(now+timedelta(minutes=5)).isoformat(),message_id,error,_now().isoformat(),update_id,token))
            if changed.rowcount!=1:
                db.commit();return _delivery_outcome(kind, AUTO_APPLIED_NOTIFICATION_UNKNOWN)
            if message_id:
                db.execute("UPDATE proposed_updates SET review_card_id=? WHERE update_id=?",(message_id,update_id))
            db.commit()
    except Exception:
        if conn is not None: conn.rollback()
        logger.exception("Could not finalize delivery receipt for %s",update_id)
        return _delivery_outcome(kind, AUTO_APPLIED_NOTIFICATION_UNKNOWN)
    try:
        from src.events.bus import Event, event_bus
        event_bus.publish(Event(event_type="email_notification_updated", project_id=row["project_id"],
            surface="email", detail={"update_id": update_id, "state": state}))
    except Exception:
        logger.exception("Notification delivery refresh event failed for %s", update_id)
    return _delivery_outcome(kind, outcome)


def process_pending_update_notifications(*, limit=10):
    from src.db.connection import scoped_write
    with scoped_write() as conn:
        recover_expired_notifications(conn)
        rows=conn.execute("SELECT update_id FROM email_update_notification_outbox WHERE state='pending' AND available_at<=? ORDER BY available_at,update_id LIMIT ?",(_now().isoformat(),max(0,min(limit,50)))).fetchall()
    return [deliver_update_notification(row[0]) for row in rows]


class NotificationReviewError(ValueError):
    """A notification review is stale, ineligible, or missing required evidence."""


def notification_status(conn, *, project_id, update_id):
    """Return delivery metadata only; do not reveal the stored message payload."""
    row = conn.execute(
        "SELECT update_id,project_id,state,attempts,available_at,telegram_message_id,"
        "last_error,updated_at,COALESCE(json_extract(payload_json, '$._notification_kind'),'update_undo') AS notification_kind FROM email_update_notification_outbox "
        "WHERE project_id=? AND update_id=?", (project_id, update_id),
    ).fetchone()
    return dict(row) if row else None


def reconcile_update_notification(conn, *, project_id, update_id, actor_id,
                                  expected_updated_at, decision, reason,
                                  telegram_message_id=None):
    """Record an explicit operator delivery decision; caller owns transaction.

This does not independently prove remote delivery. The operator's confirmation
and supporting reason are auditable. Never retry an uncertain send implicitly.
"""
    if not str(actor_id or "").strip() or not str(reason or "").strip():
        raise NotificationReviewError("actor and review reason required")
    if decision not in {"confirm_sent", "confirm_not_sent", "cancel", "bind_current_destination"}:
        raise NotificationReviewError("unsupported notification decision")
    row = notification_status(conn, project_id=project_id, update_id=update_id)
    if row is None:
        raise KeyError(update_id)
    if row["updated_at"] != expected_updated_at:
        raise NotificationReviewError("stale notification review")
    if row["state"] not in {"uncertain", "pending"}:
        raise NotificationReviewError("notification is not reviewable")
    if decision == "confirm_sent" and (
        not isinstance(telegram_message_id, int) or isinstance(telegram_message_id, bool)
        or telegram_message_id <= 0
    ):
        raise NotificationReviewError("positive Telegram message id required")
    if decision != "confirm_sent" and telegram_message_id is not None:
        raise NotificationReviewError("message id is only valid for confirm_sent")
    accepted = conn.execute(
        "SELECT decision_outcome FROM proposed_updates WHERE project_id=? AND update_id=?",
        (project_id, update_id),
    ).fetchone()
    required_decision = "needs_review" if row["notification_kind"] == "review_card" else "accepted"
    if decision in {"confirm_not_sent", "bind_current_destination"} and (
        not accepted or accepted[0] != required_decision
    ):
        raise NotificationReviewError("cannot retry a resolved update")
    if decision == "bind_current_destination":
        if row["state"] != "pending":
            raise NotificationReviewError("confirm non-delivery before rebinding an uncertain notification")
        destination = _configured_destination()
        if not destination["chat_id"] or not destination["bot_fingerprint"]:
            raise NotificationReviewError("Telegram destination is not configured")
    state = {"confirm_sent": "sent", "confirm_not_sent": "pending", "cancel": "cancelled", "bind_current_destination": "pending"}[decision]
    now = _now().isoformat()
    message_id = str(telegram_message_id) if decision == "confirm_sent" else None
    changed = conn.execute(
        "UPDATE email_update_notification_outbox SET state=?,available_at=?,updated_at=?,"
        "lease_token=NULL,lease_until=NULL,telegram_message_id=?,last_error=? "
        "WHERE update_id=? AND project_id=? AND updated_at=? AND state IN ('uncertain','pending')",
        (state, now, now, message_id, "operator_" + decision,
         update_id, project_id, expected_updated_at),
    )
    if changed.rowcount != 1:
        raise NotificationReviewError("stale notification review")
    if decision == "bind_current_destination":
        stored = conn.execute("SELECT payload_json FROM email_update_notification_outbox WHERE update_id=?", (update_id,)).fetchone()
        bound_payload = _bind_payload(json.loads(stored[0]))
        conn.execute("UPDATE email_update_notification_outbox SET payload_json=? WHERE update_id=? AND project_id=?",
                     (json.dumps(bound_payload, ensure_ascii=False, sort_keys=True), update_id, project_id))
    if message_id:
        conn.execute("UPDATE proposed_updates SET review_card_id=? WHERE update_id=? AND project_id=?",
                     (message_id, update_id, project_id))
    from src.repositories.activity_log_repo import insert_activity
    insert_activity(conn, actor=actor_id, action_type="email_notification_reconciled",
        project_id=project_id, surface="dashboard", object_ref=update_id,
        summary=f"Operator notification decision: {decision}", touch_project=False,
        metadata_json=json.dumps({"reason": reason.strip(), "previous_state": row["state"],
                                  "telegram_message_id": message_id}),
    )
    return notification_status(conn, project_id=project_id, update_id=update_id)
