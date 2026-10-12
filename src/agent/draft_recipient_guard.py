"""Deterministic draft recipient policy — block internal to_email when ball is External."""

from __future__ import annotations

import sqlite3

# Domains that are Demo Connectivity / internal ops — not external clients for External-ball drafts.
INTERNAL_DOMAIN_SUFFIXES = (
    "demo.invalid",
    "acceptance.demo.invalid",
    "exception.demo.invalid",
    "provisioning.demo.invalid",
)


def is_internal_email(email: str | None) -> bool:
    if not email or "@" not in email:
        return False
    domain = email.rsplit("@", 1)[-1].lower().strip()
    for suffix in INTERNAL_DOMAIN_SUFFIXES:
        if domain == suffix or domain.endswith("." + suffix):
            return True
    return False


def _project_ball_with(conn: sqlite3.Connection, project_id: str) -> str | None:
    row = conn.execute(
        "SELECT ball_with FROM projects WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    if not row:
        return None
    val = row["ball_with"] if hasattr(row, "keys") else row[0]
    return str(val) if val is not None else None


def suggest_external_recipients(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    limit: int = 8,
) -> list[str]:
    """Suggest external emails from contacts and recent inbound senders."""
    seen: set[str] = set()
    out: list[str] = []

    def _add(email: str | None) -> None:
        if not email or "@" not in email:
            return
        e = email.lower().strip()
        if e in seen or is_internal_email(e):
            return
        seen.add(e)
        out.append(e)

    # Contacts linked via response_pattern or contact emails on project history
    try:
        for row in conn.execute(
            """SELECT primary_email FROM contacts
               WHERE contact_id IN (
                   SELECT contact_id FROM response_pattern_memory WHERE project_id = ?
               )
               OR cross_project_history LIKE ?
               LIMIT 20""",
            (project_id, f"%{project_id}%"),
        ):
            _add(row["primary_email"] if hasattr(row, "keys") else row[0])
    except Exception:
        pass

    try:
        for row in conn.execute(
            """SELECT DISTINCT sender_email FROM processed_emails
               WHERE project_id = ? AND direction = 'inbound'
                 AND sender_email IS NOT NULL AND sender_email != ''
               ORDER BY processed_at DESC LIMIT 30""",
            (project_id,),
        ):
            _add(row["sender_email"] if hasattr(row, "keys") else row[0])
    except Exception:
        pass

    return out[:limit]


def check_draft_recipient(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    to_email: str | None,
    allow_trusted_internal_handoff: bool = False,
) -> dict:
    """
    If project ball_with is External and to_email is internal, block.

    Returns {"ok": True} or
    {"ok": False, "error": str, "suggested_external": list[str]}.
    """
    if not project_id:
        return {"ok": True}

    ball = _project_ball_with(conn, project_id)
    if not ball:
        return {"ok": True}

    ball_norm = ball.strip().lower()
    if ball_norm not in ("external", "client", "counterparty"):
        # BallWith.EXTERNAL value is typically "External"
        if "external" not in ball_norm:
            return {"ok": True}

    if not to_email:
        return {"ok": True}

    if not is_internal_email(to_email):
        return {"ok": True}

    if allow_trusted_internal_handoff:
        return {"ok": True}

    suggestions = suggest_external_recipients(conn, project_id)
    return {
        "ok": False,
        "error": (
            f"to_email '{to_email}' is an internal domain but project {project_id} "
            f"has ball_with={ball}. Draft the client/vendor, not the latest internal inbound."
        ),
        "suggested_external": suggestions,
        "hint": (
            "ball_with is External; draft should go to client/vendor, "
            "not internal latest inbound."
        ),
    }
