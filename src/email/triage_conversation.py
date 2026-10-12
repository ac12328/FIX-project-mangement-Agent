"""Future-only unresolved conversations grounded in explicit email headers.

Existing source rows and confirmed Telegram receipts are the durable ledger.
A new source remains visible; grouping never means assignment or project creation.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3

from pydantic import BaseModel, Field

UNRESOLVED_RESULTS = {"new_inquiry", "requires_operator_triage", "llm_failed", "ambiguous"}


class TriageAssignmentScope(BaseModel):
    root_email_id: str
    email_ids: list[str] = Field(default_factory=list)
    source_results: dict[str, str] = Field(default_factory=dict)
    destination_scope: str


def destination_scope() -> tuple[int | None, str | None]:
    from src.email.project_advisory_outbox import _destination
    chat_id, fingerprint = _destination()
    if not chat_id or not fingerprint:
        return None, None
    return chat_id, hashlib.sha256(f"{chat_id}:{fingerprint}".encode()).hexdigest()


def _message_id(value: str | None) -> str:
    return (value or "").strip().strip("<>")


def _headers(row: dict) -> set[str]:
    values = set()
    for raw in (row.get("in_reply_to"), row.get("references_chain")):
        # Stored References are RFC Message-IDs, not subjects or domain names.
        for value in re.findall(r"<([^<>]+)>|([^\s<>]+)", raw or ""):
            token = value[0] or value[1]
            if token:
                values.add(token)
    return values


def connected_unassigned_sources(conn: sqlite3.Connection, email_id: str) -> list[dict]:
    source = conn.execute("SELECT * FROM processed_emails WHERE email_id=?", (email_id,)).fetchone()
    if source is None:
        return []
    source = dict(source)
    if (source["project_id"] or source["ticket_id"]
            or source["result"] not in UNRESOLVED_RESULTS or source["direction"] != "inbound"):
        return []
    candidates = [dict(r) for r in conn.execute("""SELECT * FROM processed_emails
        WHERE project_id IS NULL AND ticket_id IS NULL AND direction='inbound'
          AND result IN ('new_inquiry','requires_operator_triage','llm_failed','ambiguous')
        ORDER BY received_at,email_id LIMIT 1001""")]
    # Large or incomplete inventories fail open to an individual source card.
    if len(candidates) > 1000:
        return [source]
    members = {email_id: source}
    changed = True
    while changed:
        changed = False
        ids = {_message_id(r["email_id"]) for r in members.values()}
        refs = set().union(*(_headers(r) for r in members.values()))
        threads = {r["thread_id"] for r in members.values() if r["thread_id"]}
        for row in candidates:
            if row["email_id"] in members:
                continue
            headers = _headers(row)
            if ((row["thread_id"] and row["thread_id"] in threads)
                    or _message_id(row["email_id"]) in refs
                    or headers.intersection(ids | refs)):
                members[row["email_id"]] = row
                changed = True
    return sorted(members.values(), key=lambda r: (r["received_at"] or "", r["email_id"]))


def _confirmed_cards(conn, email_ids: list[str], chat_id: int, scope: str) -> list[dict]:
    if not email_ids:
        return []
    marks = ",".join("?" for _ in email_ids)
    rows = conn.execute(f"""SELECT m.*,map.object_ref AS source_email_id
        FROM telegram_messages m JOIN message_object_map map
          ON map.chat_id=m.chat_id AND map.telegram_message_id=m.telegram_message_id
        WHERE m.direction='outbound' AND m.telegram_message_id>0 AND m.chat_id=?
          AND map.object_type='email' AND map.object_ref IN ({marks})
        ORDER BY m.id""", (chat_id, *email_ids)).fetchall()
    cards = []
    for row in rows:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except (ValueError, TypeError):
            continue
        if (isinstance(metadata, dict) and metadata.get("card_profile") == "B"
                and metadata.get("triage_destination_scope") == scope):
            cards.append({**dict(row), "metadata": metadata})
    return cards


def group_triage_notification(conn, event, body: str, notification: dict) -> dict:
    """Change a repeated request into a source update only after confirmed delivery."""
    chat_id, scope = destination_scope()
    members = connected_unassigned_sources(conn, event.email_id)
    if not scope or not members:
        return notification  # Legacy callers without saved sources remain per-email.
    cards = _confirmed_cards(conn, [r["email_id"] for r in members if r["email_id"] != event.email_id], chat_id, scope)
    root = cards[0]["metadata"].get("triage_conversation_root", cards[0]["source_email_id"]) if cards else event.email_id
    # Do not follow a stale root to a resolved/conflicting source.
    if root not in {r["email_id"] for r in members}:
        cards, root = [], event.email_id
    metadata = {"triage_destination_scope": scope, "triage_conversation_root": root,
                "triage_source_update": bool(cards)}
    notification = {**notification, "email_id": event.email_id,
                    "notification_metadata": metadata}
    if not cards:
        return notification
    from src.email.body_cleaner import clean_email_body
    from src.telegram.operator_messages import en
    lines = [("This is new mail in the same unresolved conversation, not a second classification task."
              if en() else "這是同一待分類對話的新郵件，並非另一項分類工作。"),
             ("Assignment still requires your confirmation; no project has been created."
              if en() else "仍需你確認所屬專案；未自動指派或建立專案。"),
             f"{'From' if en() else '寄件人'}：{event.sender_email}",
             f"{'Subject' if en() else '主旨'}：{event.subject}", "",
             "Newest source excerpt (attachment contents not included):" if en() else "最新郵件原文摘錄（不含附件內容）：",
             clean_email_body(body)[:850] or ("Open the original email." if en() else "請查看原郵件。"), "",
             "Choose project to review the exact saved sources before assignment." if en() else "選擇所屬專案時，會先列出本次將指派的確切郵件。"]
    # Keep a classification entry point: a previous card may have been resolved
    # by viewing DETAILS, which removes its keyboard in the existing lifecycle.
    return {**notification, "title": "Unresolved conversation · new source" if en() else "待分類對話・新增郵件",
            "body": "\n".join(lines), "buttons": [["ASSIGN", "DETAILS"]]}


def freeze_assignment_scope(conn, email_id: str, context: dict) -> TriageAssignmentScope | None:
    """Only positively delivered future sources may join a confirmed assignment."""
    from src.telegram._cb_helpers import extract_message_metadata
    metadata = extract_message_metadata(context)
    chat_id, scope = destination_scope()
    if (not scope or metadata.get("triage_destination_scope") != scope
            or context.get("chat_id") != chat_id):
        return None
    members = connected_unassigned_sources(conn, email_id)
    cards = _confirmed_cards(conn, [r["email_id"] for r in members], chat_id, scope)
    root = metadata.get("triage_conversation_root")
    delivered = {c["source_email_id"] for c in cards
                 if c["metadata"].get("triage_conversation_root") == root}
    selected = [r for r in members if r["email_id"] in delivered]
    if not selected or root not in {r["email_id"] for r in selected}:
        return None
    return TriageAssignmentScope(root_email_id=root,
        email_ids=[r["email_id"] for r in selected],
        source_results={r["email_id"]: r["result"] for r in selected}, destination_scope=scope)


def assignment_prompt(conn, scope: TriageAssignmentScope) -> str:
    from src.telegram.operator_messages import en
    lines = [f"{'Sources in this assignment' if en() else '本次指派範圍'}：{len(scope.email_ids)}",
             "Later arrivals are not included. No changes yet. Reply with an existing project ID."
             if en() else "不包括之後收到的郵件；目前未有更改。請回覆現有專案編號。"]
    for email_id in scope.email_ids:
        row = conn.execute("SELECT subject FROM processed_emails WHERE email_id=?", (email_id,)).fetchone()
        lines.append(f"• {email_id} | {(row[0] or '')[:100]}")
    return "\n".join(lines)


def apply_frozen_assignment(conn, scope: TriageAssignmentScope, project_id: str, actor: str) -> int:
    """All-or-nothing source assignment; never reclassify conflicting or late sources."""
    from src.db.savepoint import savepoint
    from src.repositories.activity_log_repo import insert_activity
    if destination_scope()[1] != scope.destination_scope:
        raise ValueError("classification destination changed")
    if not conn.execute("SELECT 1 FROM projects WHERE project_id=?", (project_id,)).fetchone():
        raise ValueError("assignment project no longer exists")
    with savepoint(conn, ensure_transaction=True):
        for email_id in scope.email_ids:
            changed = conn.execute("""UPDATE processed_emails SET project_id=?,result='project_update'
                WHERE email_id=? AND project_id IS NULL AND ticket_id IS NULL
                  AND result=? AND direction='inbound'""",
                (project_id, email_id, scope.source_results[email_id])).rowcount
            if changed != 1:
                raise ValueError("assignment scope changed; review sources again")
            insert_activity(conn, actor=actor, surface="telegram", project_id=project_id,
                action_type="triage_assigned", object_ref=email_id,
                summary=f"Explicitly assigned source to {project_id}",
                metadata_json=json.dumps({"conversation_root": scope.root_email_id,
                                          "assignment_email_ids": scope.email_ids}))
    return len(scope.email_ids)
