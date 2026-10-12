"""Learned rules management endpoints."""

import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.api.deps import get_conn, get_write_conn
from src.api.schemas.common import ActionResult
from src.repositories.learned_rules_repo import (
    approve_rule,
    bulk_approve_rules,
    bulk_reject_rules,
    get_all_learned_rules,
    get_applicable_rules,
    get_pending_rules,
    learning_sink_metrics,
    reject_rule,
)

router = APIRouter(tags=["learned_rules"])


class RuleActionRequest(BaseModel):
    action: str  # approve | reject


class BatchRuleActionRequest(BaseModel):
    action: str  # approve | reject
    rule_ids: list[str] = Field(default_factory=list)


@router.get("/learned-rules")
def list_all_rules(conn: sqlite3.Connection = Depends(get_conn)):
    """Return all learned rules (any status)."""
    return get_all_learned_rules(conn)


@router.get("/learned-rules/pending")
def list_pending(conn: sqlite3.Connection = Depends(get_conn)):
    """Return rules awaiting operator review."""
    return get_pending_rules(conn)


@router.get("/learned-rules/active")
def list_active(
    market: str | None = None,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Return approved rules currently injected into agent prompts."""
    return get_applicable_rules(conn, market=market)


@router.get("/learned-rules/metrics")
def rules_metrics(conn: sqlite3.Connection = Depends(get_conn)):
    """Learning sink health counts (injectable, pending, feedback, patterns)."""
    return learning_sink_metrics(conn)


@router.post("/learned-rules/batch-action", response_model=ActionResult)
def batch_rule_action(
    body: BatchRuleActionRequest,
    conn: sqlite3.Connection = Depends(get_write_conn),
):
    if not body.rule_ids:
        raise HTTPException(400, "rule_ids required")
    if body.action == "approve":
        n = bulk_approve_rules(conn, body.rule_ids)
        conn.commit()
        return ActionResult(success=True, message=f"Approved {n} rule(s)")
    if body.action == "reject":
        n = bulk_reject_rules(conn, body.rule_ids)
        conn.commit()
        return ActionResult(success=True, message=f"Rejected {n} rule(s)")
    raise HTTPException(400, f"Unknown action: {body.action}")


@router.post("/learned-rules/{rule_id}/action", response_model=ActionResult)
def rule_action(
    rule_id: str,
    body: RuleActionRequest,
    conn: sqlite3.Connection = Depends(get_write_conn),
):
    if body.action == "approve":
        ok = approve_rule(conn, rule_id)
        if not ok:
            raise HTTPException(404, "Rule not found or not pending")
        conn.commit()
        return ActionResult(success=True, message=f"Rule {rule_id} approved")

    if body.action == "reject":
        ok = reject_rule(conn, rule_id)
        if not ok:
            raise HTTPException(404, "Rule not found or not pending")
        conn.commit()
        return ActionResult(success=True, message=f"Rule {rule_id} rejected")

    raise HTTPException(400, f"Unknown action: {body.action}")
