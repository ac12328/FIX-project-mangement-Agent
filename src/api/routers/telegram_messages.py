"""Telegram message inspection endpoints."""

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from src.api.deps import get_conn
from src.telegram.persistence import list_project_messages

router = APIRouter(tags=["telegram"])


@router.get("/projects/{project_id}/telegram-messages")
def get_project_telegram_messages(
    project_id: str,
    limit: int = Query(50, ge=1, le=200),
    object_type: str | None = Query(None),
    object_ref: str | None = Query(None),
    conn: sqlite3.Connection = Depends(get_conn),
):
    return list_project_messages(
        conn,
        project_id,
        limit=limit,
        object_type=object_type,
        object_ref=object_ref,
    )


class TelegramSendRequest(BaseModel):
    text: str
    chat_id: int | None = None
    project_id: str | None = None


@router.post("/telegram/send")
def send_telegram_message(
    body: TelegramSendRequest,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Send a message to the operator's Telegram chat (Dashboard → Telegram push)."""
    import requests as req_lib

    from config.settings import CONFIG

    token = CONFIG.telegram.bot_token
    if not token:
        raise HTTPException(status_code=503, detail="Telegram bot not configured")

    chat_id = body.chat_id or CONFIG.telegram.chat_id
    if not chat_id:
        raise HTTPException(status_code=503, detail="No chat_id configured")

    from src.telegram.message_delivery import resolve_format_mode
    from src.telegram.stream_format import markdown_to_telegram_html, sanitize_llm_markdown

    source = sanitize_llm_markdown(body.text or "")
    mode = resolve_format_mode()

    # Rich only when explicitly enabled — many clients show
    # "This message is not supported by your version of Telegram".
    if mode == "rich":
        rich_resp = req_lib.post(
            f"https://api.telegram.org/bot{token}/sendRichMessage",
            json={"chat_id": chat_id, "rich_message": {"markdown": source}},
            timeout=10,
        )
        if rich_resp.ok:
            data = rich_resp.json()
            return {
                "ok": True,
                "message_id": data.get("result", {}).get("message_id"),
                "render": "rich",
            }

    html_body = markdown_to_telegram_html(source)
    resp = req_lib.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": html_body,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=10,
    )
    if not resp.ok:
        plain = req_lib.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": source},
            timeout=10,
        )
        if not plain.ok:
            raise HTTPException(status_code=502, detail=f"Telegram API error: {resp.text[:200]}")
        data = plain.json()
        return {"ok": True, "message_id": data.get("result", {}).get("message_id"), "render": "plain"}

    data = resp.json()
    return {"ok": True, "message_id": data.get("result", {}).get("message_id"), "render": "html"}
