"""Immutable exact memory exposure repository."""
from __future__ import annotations

import hashlib
import sqlite3
from typing import Iterable

from pydantic import BaseModel, ConfigDict, Field


class ExposureCandidate(BaseModel):
    model_config = ConfigDict(frozen=True)
    fact_id: str
    fact_version: int = Field(ge=1)
    rendered_hash: str
    rendered_text: str = ""
    rank: int = Field(ge=1)
    token_count: int = Field(ge=0)


class MemoryExposure(ExposureCandidate):
    model_config = ConfigDict(frozen=True)
    trace_id: str
    output_revision_id: str
    operator_id: str
    surface: str
    source_surface: str | None = None
    task_type: str | None = None
    project_id: str | None = None
    exposure_id: str | None = None


def exposure_id_for(item: MemoryExposure) -> str:
    raw = "|".join((item.trace_id, item.output_revision_id, item.fact_id, str(item.fact_version)))
    return "mx_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def insert_exposures(conn: sqlite3.Connection, exposures: Iterable[MemoryExposure]) -> list[str]:
    ids: list[str] = []
    for item in exposures:
        exposure_id = item.exposure_id or exposure_id_for(item)
        conn.execute(
            """INSERT OR IGNORE INTO memory_exposures
               (exposure_id,trace_id,output_revision_id,operator_id,surface,source_surface,
                task_type,project_id,fact_id,fact_version,rendered_hash,rank,token_count)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (exposure_id, item.trace_id, item.output_revision_id, item.operator_id,
             item.surface, item.source_surface or item.surface, item.task_type,
             item.project_id, item.fact_id, item.fact_version, item.rendered_hash,
             item.rank, item.token_count),
        )
        ids.append(exposure_id)
    return ids


def list_exposures(
    conn: sqlite3.Connection, *, trace_id: str, output_revision_id: str,
    operator_id: str | None = None, surface: str | None = None,
) -> list[MemoryExposure]:
    sql = "SELECT * FROM memory_exposures WHERE trace_id=? AND output_revision_id=?"
    args: list[object] = [trace_id, output_revision_id]
    if operator_id is not None:
        sql += " AND operator_id=?"
        args.append(operator_id)
    if surface is not None:
        sql += " AND surface=?"
        args.append(surface)
    sql += " ORDER BY rank,fact_id,fact_version"
    rows = conn.execute(sql, args).fetchall()
    return [MemoryExposure(**dict(row), rendered_text="") for row in rows]
