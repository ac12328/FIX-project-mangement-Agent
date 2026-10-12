"""Shared types and gates for Workspace operator updates."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal

from config import defaults
from src.governance.four_eyes import UNBOUND_ACTORS, is_bound_actor
from src.repositories.evidence_repo import get_evidence_by_id

_DASHBOARD_WRITE_ROLES = frozenset({"Admin", "Editor"})

V1_FIELDS = frozenset({"current_milestone", "next_action"})
OVERRIDE_PREFIX = "operator_override: "
_BOILERPLATE = frozenset({"override", "operator override", "n/a", "because"})
Transition = Literal["adjacent", "jump", "revert", "noop"]
Kind = Literal["evaluator_aligned", "operator_override"]


class OperatorUpdateError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class OperatorUpdateProposal:
    project_id: str
    fields: dict[str, str]
    reason: str
    evidence_ref: str | None
    kind: Kind
    assessment: dict
    missing_criteria: list[dict]
    current_values: dict
    base_version: str
    high_risk: bool
    high_risk_reason: str | None
    transition: Transition
    assessment_status: str
    policy_version: str | None
    decision: str | None
    candidate_value: str | None


def is_usable_override_reason(text: str) -> bool:
    normalized = " ".join((text or "").split()).strip()
    if len(normalized) < 20:
        return False
    folded = normalized.casefold()
    if folded in _BOILERPLATE or folded == "operator_override:":
        return False
    return True


def require_flag() -> None:
    if not bool(getattr(defaults, "WORKSPACE_OPERATOR_WRITES_ENABLED", False)):
        raise OperatorUpdateError(
            "flag_disabled",
            "Workspace operator writes are disabled.",
        )


def is_workspace_write_actor(conn: sqlite3.Connection, actor: str) -> bool:
    """True when the actor is a named person who can confirm Workspace writes.

    ``is_bound_actor`` treats the shipped ``default_operator`` identity as
    unbound so Telegram/system fallbacks cannot self-approve. The primary
    Dashboard Admin is bootstrapped with that same user_id, so an active
    Admin/Editor row is accepted here. Generic actors such as ``operator``
    stay rejected even if a colliding user row exists.
    """
    if is_bound_actor(actor):
        return True
    value = (actor or "").strip()
    if not value or value in UNBOUND_ACTORS:
        return False
    try:
        row = conn.execute(
            "SELECT role, status FROM dashboard_users WHERE user_id=?",
            (value,),
        ).fetchone()
    except sqlite3.Error:
        return False
    if row is None:
        return False
    return str(row["status"]) == "active" and str(row["role"]) in _DASHBOARD_WRITE_ROLES


def require_bound_actor(conn: sqlite3.Connection, actor: str) -> None:
    if not is_workspace_write_actor(conn, actor):
        raise OperatorUpdateError(
            "unbound_dashboard_actor",
            "Sign in as a Dashboard Editor to apply Workspace milestone updates.",
        )


def audit_reason(kind: Kind, reason: str) -> str:
    text = " ".join((reason or "").split()).strip()
    if kind == "operator_override":
        return f"{OVERRIDE_PREFIX}{text}"
    return text


def request_key(
    actor: str, thread_id: str, project_id: str, to_milestone: str, base_version: str,
) -> str:
    return f"ws-op-update:{actor}:{thread_id}:{project_id}:{to_milestone}:{base_version}"


def canonical_evidence_ref(
    conn: sqlite3.Connection, project_id: str, evidence_ref: str | None,
) -> str | None:
    if not evidence_ref:
        return None
    raw = evidence_ref.strip()
    email_id = None
    if raw.lower().startswith("email:"):
        email_id = raw.split(":", 1)[1]
    elif raw.lower().startswith("processed_email:"):
        email_id = raw.split(":", 1)[1]
    if email_id:
        row = conn.execute(
            "SELECT email_id FROM processed_emails WHERE email_id=? AND project_id=?",
            (email_id, project_id),
        ).fetchone()
        if row is None:
            raise OperatorUpdateError(
                "evidence_required",
                f"Email evidence {email_id} is not linked to {project_id}.",
            )
        return f"email:{email_id}"
    item = get_evidence_by_id(conn, raw)
    if item is None or item.project_id != project_id:
        raise OperatorUpdateError(
            "evidence_required",
            f"Evidence {raw} is not linked to {project_id}.",
        )
    return raw
