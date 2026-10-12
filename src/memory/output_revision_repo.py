"""Immutable output revision persistence and exact artifact lookup."""
from __future__ import annotations

import hashlib
import sqlite3
from typing import Any


def revision_id_for(surface: str, artifact_type: str, artifact_id: str, revision: int) -> str:
    raw = f"{surface}|{artifact_type}|{artifact_id}|{revision}"
    return "or_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def create_output_revision(
    conn: sqlite3.Connection,
    *,
    operator_id: str,
    surface: str,
    artifact_type: str,
    artifact_id: str,
    revision: int,
    trace_id: str | None,
    content: str,
    output_revision_id: str | None = None,
    prior_output_revision_id: str | None = None,
) -> str:
    """Insert one immutable artifact revision; repeated exact inserts are idempotent."""
    revision_id = output_revision_id or revision_id_for(
        surface, artifact_type, artifact_id, revision,
    )
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    conn.execute(
        """INSERT OR IGNORE INTO output_revisions
           (output_revision_id,operator_id,surface,artifact_type,artifact_id,revision,
            trace_id,content_hash,prior_output_revision_id)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (revision_id, operator_id, surface, artifact_type, artifact_id, revision,
         trace_id, content_hash, prior_output_revision_id),
    )
    row = conn.execute(
        "SELECT * FROM output_revisions WHERE output_revision_id=?", (revision_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError("output revision insert failed")
    values = dict(row)
    expected = {
        "operator_id": operator_id, "surface": surface,
        "artifact_type": artifact_type, "artifact_id": artifact_id,
        "revision": revision, "trace_id": trace_id, "content_hash": content_hash,
        "prior_output_revision_id": prior_output_revision_id,
    }
    if any(values.get(key) != value for key, value in expected.items()):
        raise sqlite3.IntegrityError("output revision identity/content mismatch")
    return revision_id


def get_output_revision_for_artifact(
    conn: sqlite3.Connection, *, surface: str, artifact_type: str,
    artifact_id: str, revision: int,
) -> dict[str, Any] | None:
    row = conn.execute(
        """SELECT * FROM output_revisions
           WHERE surface=? AND artifact_type=? AND artifact_id=? AND revision=?""",
        (surface, artifact_type, artifact_id, revision),
    ).fetchone()
    return dict(row) if row else None


def get_output_revision(conn: sqlite3.Connection, output_revision_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM output_revisions WHERE output_revision_id=?", (output_revision_id,),
    ).fetchone()
    return dict(row) if row else None


def bind_inference_revision_to_artifact(
    conn: sqlite3.Connection, *, inference_output_revision_id: str, trace_id: str,
    operator_id: str, surface: str, artifact_type: str, artifact_id: str,
    revision: int, content: str,
) -> str:
    """Bind an artifact to one exact inference revision and copy its exposures."""
    inference = get_output_revision(conn, inference_output_revision_id)
    if not inference or any((
        inference["trace_id"] != trace_id,
        inference["operator_id"] != operator_id,
        inference["surface"] != surface,
    )):
        raise sqlite3.IntegrityError("inference revision identity mismatch")
    new_id = create_output_revision(
        conn, operator_id=operator_id, surface=surface, artifact_type=artifact_type,
        artifact_id=artifact_id, revision=revision, trace_id=trace_id, content=content,
        prior_output_revision_id=inference_output_revision_id,
    )
    rows = conn.execute(
        "SELECT * FROM memory_exposures WHERE output_revision_id=?",
        (inference_output_revision_id,),
    ).fetchall()
    from src.memory.exposure_repo import MemoryExposure, insert_exposures
    insert_exposures(conn, [MemoryExposure(
        trace_id=trace_id, output_revision_id=new_id,
        operator_id=row["operator_id"], surface=surface,
        source_surface=row["source_surface"], task_type=row["task_type"],
        project_id=row["project_id"], fact_id=row["fact_id"],
        fact_version=row["fact_version"], rendered_hash=row["rendered_hash"],
        rank=row["rank"], token_count=row["token_count"], rendered_text="",
    ) for row in rows])
    return new_id


def inherit_revision_exposures(
    conn: sqlite3.Connection, *, source_output_revision_id: str,
    target_output_revision_id: str,
) -> None:
    """Persist explicit exposure lineage on an operator-edited artifact revision."""
    source = get_output_revision(conn, source_output_revision_id)
    target = get_output_revision(conn, target_output_revision_id)
    if not source or not target or not source.get("trace_id"):
        return
    rows = conn.execute(
        "SELECT * FROM memory_exposures WHERE output_revision_id=?",
        (source_output_revision_id,),
    ).fetchall()
    from src.memory.exposure_repo import MemoryExposure, insert_exposures
    insert_exposures(conn, [MemoryExposure(
        trace_id=source["trace_id"], output_revision_id=target_output_revision_id,
        operator_id=target["operator_id"], surface=target["surface"],
        source_surface=row["source_surface"], task_type=row["task_type"],
        project_id=row["project_id"], fact_id=row["fact_id"],
        fact_version=row["fact_version"], rendered_hash=row["rendered_hash"],
        rank=row["rank"], token_count=row["token_count"], rendered_text="",
    ) for row in rows])


# Compatibility alias for callers that already supply an exact trace but not the revision.
def bind_trace_revision_to_artifact(
    conn: sqlite3.Connection, *, trace_id: str, operator_id: str, surface: str,
    artifact_type: str, artifact_id: str, revision: int, content: str,
) -> str:
    rows = conn.execute(
        """SELECT output_revision_id FROM output_revisions
           WHERE trace_id=? AND operator_id=? AND surface=? AND artifact_type='inference'""",
        (trace_id, operator_id, surface),
    ).fetchall()
    if len(rows) != 1:
        raise sqlite3.IntegrityError("exact inference revision required")
    return bind_inference_revision_to_artifact(
        conn, inference_output_revision_id=rows[0][0], trace_id=trace_id,
        operator_id=operator_id, surface=surface, artifact_type=artifact_type,
        artifact_id=artifact_id, revision=revision, content=content,
    )
