"""Canonical FIX session registry endpoints."""
from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.api.auth import CurrentActor
from src.api.deps import get_conn, get_write_actor, get_write_conn
from src.fix_sessions.repository import list_candidates, list_history, list_sessions, upsert_sender_policy
from src.fix_sessions.service import approve_candidate, reject_candidate

router = APIRouter(tags=["fix-sessions"])


class DecisionRequest(BaseModel):
    reason: str = Field(min_length=1)


class SenderPolicyRequest(BaseModel):
    email_address: str = Field(min_length=3)
    approved_by: str = Field(min_length=1)
    organisation_id: str | None = None
    auto_confirm_enabled: bool = True
    notes: str | None = None


@router.get("/projects/{project_id}/fix-sessions")
def current_sessions(project_id: str, conn: sqlite3.Connection = Depends(get_conn)) -> list[dict[str, Any]]:
    return list_sessions(conn, project_id)


@router.get("/projects/{project_id}/fix-sessions/{environment}/{session_key}/history")
def session_history(project_id: str, environment: str, session_key: str, conn: sqlite3.Connection = Depends(get_conn)) -> list[dict[str, Any]]:
    return list_history(conn, project_id, environment, session_key)


@router.get("/fix-session-candidates")
def pending_candidates(project_id: str | None = None, conn: sqlite3.Connection = Depends(get_conn)) -> list[dict[str, Any]]:
    return list_candidates(conn, project_id)


@router.post("/fix-session-candidates/{candidate_id}/approve")
def approve(
    candidate_id: str,
    payload: DecisionRequest,
    actor: CurrentActor = Depends(get_write_actor),
    conn: sqlite3.Connection = Depends(get_write_conn),
) -> dict[str, str]:
    try:
        version_id = approve_candidate(conn, candidate_id, operator_id=actor.operator_id, reason=payload.reason)
    except KeyError:
        raise HTTPException(status_code=404, detail="Candidate not found")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"candidate_id": candidate_id, "session_version_id": version_id, "status": "confirmed"}


@router.post("/fix-session-candidates/{candidate_id}/reject")
def reject(
    candidate_id: str,
    payload: DecisionRequest,
    actor: CurrentActor = Depends(get_write_actor),
    conn: sqlite3.Connection = Depends(get_write_conn),
) -> dict[str, str]:
    try:
        reject_candidate(conn, candidate_id, operator_id=actor.operator_id, reason=payload.reason)
    except KeyError:
        raise HTTPException(status_code=404, detail="Candidate not found")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"candidate_id": candidate_id, "status": "rejected"}


@router.post("/fix-session-sender-policies")
def save_sender_policy(payload: SenderPolicyRequest, conn: sqlite3.Connection = Depends(get_write_conn)) -> dict[str, str]:
    policy_id = upsert_sender_policy(conn, **payload.model_dump())
    return {"policy_id": policy_id, "status": "active"}
