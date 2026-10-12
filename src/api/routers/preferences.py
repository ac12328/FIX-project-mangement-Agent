"""Operator preferences endpoints."""

import sqlite3
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from src.api.deps import get_conn, get_write_conn
from src.repositories.preference_repo import (
    OperatorPreference,
    list_preferences,
    upsert_preference,
)

router = APIRouter(tags=["preferences"])


class PreferenceUpsertRequest(BaseModel):
    scope: str  # "project" | "counterparty"
    project_id: str | None = None
    counterparty_id: str | None = None
    tone: str = "semi-formal"
    language: str = "bilingual"
    push_style: str = "reactive"
    custom_notes: str | None = None


@router.get("/preferences")
def get_preferences(conn: sqlite3.Connection = Depends(get_conn)):
    """List all operator preferences."""
    rows = list_preferences(conn)
    return [_pref_to_dict(p, conn) for p in rows]


@router.post("/preferences", status_code=201)
def create_or_update_preference(
    body: PreferenceUpsertRequest,
    conn: sqlite3.Connection = Depends(get_write_conn),
):
    """Create or update an operator preference."""
    if body.scope not in ("project", "counterparty"):
        raise HTTPException(400, "scope must be 'project' or 'counterparty'")
    pref = OperatorPreference(
        scope=body.scope,
        project_id=body.project_id,
        counterparty_id=body.counterparty_id,
        tone=body.tone,
        language=body.language,
        push_style=body.push_style,
        custom_notes=body.custom_notes,
    )
    row_id = upsert_preference(conn, pref)
    return {"id": row_id, "message": "Preference saved."}


@router.delete("/preferences/{pref_id}", status_code=200)
def delete_preference(
    pref_id: int,
    conn: sqlite3.Connection = Depends(get_write_conn),
):
    """Delete an operator preference by its integer id."""
    cur = conn.execute(
        "DELETE FROM operator_preferences WHERE id = ?", (pref_id,)
    )
    conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "Preference not found")
    return {"success": True, "message": "Preference deleted."}


# ── Notification budget preferences ──────────────────────────────────────────


_DELIVERY_MODE_ALIASES = {
    "3x_daily": "digest_3x",
    "2x_daily": "digest_2x",
    "realtime": "realtime",
}

_DASHBOARD_TO_DB = {
    "max_daily": "max_daily_notifications",
    "delivery_mode": "notification_delivery_mode",
    "quiet_hours_start": "quiet_hours_start",
    "quiet_hours_end": "quiet_hours_end",
}


@router.get("/notifications")
def get_notification_prefs(conn: sqlite3.Connection = Depends(get_conn)):
    """Return notification budget status (caps, quiet hours, sent today)."""
    from src.repositories.operator_state_repo import get_notification_mode
    from src.utils.notification_budget import get_budget_status

    data = get_budget_status(conn)
    data["notification_mode"] = get_notification_mode(conn)
    dm = data.get("delivery_mode")
    if dm == "digest_3x":
        data["delivery_mode"] = "3x_daily"
    elif dm == "digest_2x":
        data["delivery_mode"] = "2x_daily"
    return data


@router.patch("/notifications")
def update_notification_prefs(body: dict, conn: sqlite3.Connection = Depends(get_write_conn)):
    """Update notification budget settings (dashboard or DB column names)."""
    from src.repositories.operator_state_repo import set_notification_mode

    db_updates: dict[str, object] = {}
    for key, val in body.items():
        if key == "notification_mode":
            set_notification_mode(conn, str(val))
            continue
        if key in _DASHBOARD_TO_DB:
            col = _DASHBOARD_TO_DB[key]
            if col == "notification_delivery_mode" and isinstance(val, str):
                val = _DELIVERY_MODE_ALIASES.get(val, val)
            db_updates[col] = val
        elif key in (
            "max_daily_notifications",
            "notification_delivery_mode",
            "quiet_hours_start",
            "quiet_hours_end",
        ):
            db_updates[key] = val

    if not db_updates and "notification_mode" not in body:
        return {"success": False, "message": "No valid fields"}

    if db_updates:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT OR IGNORE INTO operator_state (id, updated_at) VALUES (1, ?)",
            (now,),
        )
        sets = ", ".join(f"{k} = ?" for k in db_updates)
        conn.execute(
            f"UPDATE operator_state SET {sets}, updated_at = ? WHERE id = 1",
            [*db_updates.values(), now],
        )
        conn.commit()
    return {"success": True}


# ── Response language preference ───────────────────────────────────────────


@router.get("/preferences/language")
def get_language_preference(conn: sqlite3.Connection = Depends(get_conn)):
    """Return the operator's preferred response language."""
    from src.repositories.operator_state_repo import get_response_language

    return {"response_language": get_response_language(conn)}


@router.get("/preferences/agent-harness")
def get_agent_harness_prefs(conn: sqlite3.Connection = Depends(get_conn)):
    """Return Plan A agent harness toggles (DB override + config defaults)."""
    from src.repositories.operator_state_repo import get_agent_harness_prefs as _get

    return _get(conn)


@router.patch("/preferences/agent-harness")
def patch_agent_harness_prefs(
    body: dict,
    conn: sqlite3.Connection = Depends(get_write_conn),
):
    """Update harness toggles. Use reset_to_config: true to clear DB overrides."""
    from src.repositories.operator_state_repo import (
        clear_agent_harness_override,
        set_agent_harness_prefs,
    )

    if body.get("reset_to_config"):
        fields = body.get("reset_fields") or [
            "require_wiki_on_progress_advisory",
            "auto_draft_on_rule58",
        ]
        for key in fields:
            if key in ("require_wiki_on_progress_advisory", "auto_draft_on_rule58"):
                clear_agent_harness_override(conn, key)
        return {"success": True}

    req = body.get("require_wiki_on_progress_advisory")
    draft = body.get("auto_draft_on_rule58")
    if req is None and draft is None:
        return {"success": False, "message": "No valid fields"}
    if req is not None:
        req = bool(req)
    if draft is not None:
        draft = bool(draft)
    set_agent_harness_prefs(
        conn,
        require_wiki_on_progress_advisory=req,
        auto_draft_on_rule58=draft,
    )
    return {"success": True}


@router.put("/preferences/language")
def set_language_preference(
    body: dict,
    conn: sqlite3.Connection = Depends(get_write_conn),
):
    """Set the operator's preferred response language."""
    from src.repositories.operator_state_repo import (
        VALID_LANGUAGES,
        set_response_language,
    )

    lang = body.get("response_language", "")
    if lang not in VALID_LANGUAGES:
        raise HTTPException(
            400,
            f"Invalid language. Must be one of {list(VALID_LANGUAGES)}",
        )
    set_response_language(conn, lang)
    return {"success": True, "response_language": lang}


def _pref_to_dict(pref: OperatorPreference, conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        """SELECT id, updated_at FROM operator_preferences
           WHERE scope = ? AND (project_id IS ? OR project_id = ?)
             AND (counterparty_id IS ? OR counterparty_id = ?)
           LIMIT 1""",
        (pref.scope, pref.project_id, pref.project_id,
         pref.counterparty_id, pref.counterparty_id),
    ).fetchone()
    return {
        "id": row["id"] if row else None,
        "scope": pref.scope,
        "project_id": pref.project_id,
        "counterparty_id": pref.counterparty_id,
        "tone": pref.tone,
        "language": pref.language,
        "push_style": pref.push_style,
        "custom_notes": pref.custom_notes,
        "updated_at": row["updated_at"] if row else None,
    }
