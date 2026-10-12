"""Append-only persistence for human memory decisions."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class MemoryDecision(BaseModel):
    model_config = ConfigDict(frozen=True)
    decision_id: str
    candidate_id: str
    candidate_version: int
    decision_type: Literal["approve", "reject", "edit_and_approve", "narrow_scope", "quarantine", "supersede", "revoke", "expire", "revalidate"]
    actor_id: str
    reason: str
    validation_id: str
    evaluation_id: str | None = None
    replaces_fact_id: str | None = None
    decision_payload_json: str = "{}"
    decided_at: datetime


def insert_decision(conn: sqlite3.Connection, decision: MemoryDecision) -> None:
    values = decision.model_dump(mode="json")
    conn.execute(
        """INSERT INTO memory_decisions
           (decision_id,candidate_id,candidate_version,decision_type,actor_id,reason,
            validation_id,evaluation_id,replaces_fact_id,decision_payload_json,decided_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        tuple(values[name] for name in ("decision_id", "candidate_id", "candidate_version", "decision_type",
              "actor_id", "reason", "validation_id", "evaluation_id", "replaces_fact_id",
              "decision_payload_json", "decided_at")),
    )


def list_decisions(conn: sqlite3.Connection, candidate_id: str, candidate_version: int) -> list[MemoryDecision]:
    rows = conn.execute(
        "SELECT * FROM memory_decisions WHERE candidate_id=? AND candidate_version=? ORDER BY decided_at, decision_id",
        (candidate_id, candidate_version),
    ).fetchall()
    return [MemoryDecision(**dict(row)) for row in rows]
