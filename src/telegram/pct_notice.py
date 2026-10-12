"""Completion-only notification: no undo/reopen action is offered."""
import asyncio
import hashlib
from config.settings import CONFIG
from src.db.connection import scoped_read,scoped_write
from src.utils.push_guard import URGENCY_URGENT
from src.utils.notification_budget import should_push_with_budget,consume_notification


def push_pct_completed_notice_sync(conn, *, project_id, update_id, summary,
                                   chat_id, expected_bot_fingerprint):
    token=str(CONFIG.telegram.bot_token or "")
    if not token or not chat_id:
        return {"sent":False,"error":"telegram_not_configured","telegram_message_id":None}
    if hashlib.sha256(token.encode()).hexdigest()!=expected_bot_fingerprint:
        return {"sent":False,"error":"bot_identity_changed","telegram_message_id":None}
    if conn is None:
        with scoped_read() as db:allowed=should_push_with_budget(db,URGENCY_URGENT)
    else:allowed=should_push_with_budget(conn,URGENCY_URGENT)
    if not allowed:return {"sent":False,"error":"budget_exhausted","telegram_message_id":None}
    async def send(_ignored=None):
        from telegram import Bot
        from src.telegram.runtime import send_persisted_message
        ids=await send_persisted_message(Bot(token=token),None,chat_id=int(chat_id),text=summary,
            project_id=project_id,message_kind="text",
            object_links=[{"object_type":"proposed_update","object_ref":update_id,"project_id":project_id}],
            metadata={"notice":"pct_completed","review_update_id":update_id})
        return {"sent":bool(ids),"telegram_message_id":ids[-1] if ids else None}
    from src.utils.async_helpers import has_running_loop
    if has_running_loop():
        from src.telegram.card_push import _run_push_in_thread
        result=_run_push_in_thread(send)
    else:result=asyncio.run(send())
    if result.get("sent"):
        if conn is None:
            with scoped_write() as db:consume_notification(db, commit=False)
        else:consume_notification(conn)
    return result
