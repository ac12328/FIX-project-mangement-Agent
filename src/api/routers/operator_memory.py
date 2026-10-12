"""Dashboard API for operator_memory CRUD."""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.api.deps import get_read_conn, get_write_conn
from src.agent.operator_identity import resolve_operator_id
from src.memory.reform_flags import operator_memory_enabled
from src.models.operator_memory import MemoryType, OperatorMemoryEntry
from src.repositories import operator_memory_repo as repo

router = APIRouter(tags=["operator-memory"])


class OperatorMemoryPayload(BaseModel):
    scope: str = "global"
    scope_id: str = "*"
    memory_type: str = "semantic"
    memory_key: str
    memory_value: dict[str, Any] = Field(default_factory=dict)
    source: str = "dashboard"
    active: bool = True


@router.get("/operator-memory")
def list_operator_memory(
    conn: sqlite3.Connection = Depends(get_read_conn),
    scope: str | None = None,
    memory_type: str | None = None,
):
    if not operator_memory_enabled():
        return {"enabled": False, "items": []}
    op_id = resolve_operator_id(conn)
    items = repo.list_memories(conn, op_id, scope=scope, memory_type=memory_type)
    return {"enabled": True, "operator_id": op_id, "items": items}


@router.post("/operator-memory")
def upsert_operator_memory(
    payload: OperatorMemoryPayload,
    conn: sqlite3.Connection = Depends(get_write_conn),
):
    if not operator_memory_enabled():
        raise HTTPException(status_code=403, detail="operator_memory_enabled is false")
    op_id = resolve_operator_id(conn)
    entry = OperatorMemoryEntry(
        operator_id=op_id,
        scope=payload.scope,  # type: ignore[arg-type]
        scope_id=payload.scope_id,
        memory_type=MemoryType(payload.memory_type),
        memory_key=payload.memory_key,
        memory_value=payload.memory_value,
        source=payload.source,
        active=payload.active,
    )
    repo.upsert_memory(conn, entry)
    conn.commit()
    return {"ok": True, "operator_id": op_id, "memory_key": payload.memory_key}


@router.delete("/operator-memory/{memory_id}")
def deactivate_operator_memory(
    memory_id: int,
    conn: sqlite3.Connection = Depends(get_write_conn),
):
    if not operator_memory_enabled():
        raise HTTPException(status_code=403, detail="operator_memory_enabled is false")
    row = conn.execute(
        "SELECT * FROM operator_memory WHERE id = ?", (memory_id,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="not found")
    repo.deactivate_memory(
        conn,
        row["operator_id"],
        row["scope"],
        row["scope_id"],
        row["memory_type"],
        row["memory_key"],
    )
    conn.commit()
    return {"ok": True, "id": memory_id}