"""Privacy-safe persisted parity telemetry for governed memory shadow reads."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class ShadowParityRecord:
    surface: str
    operator_id: str | None
    project_id: str | None
    cohort: str
    policy_version: int | None
    legacy_text: str
    v2_text: str
    fact_versions: Iterable[tuple[str, int]] = ()
    trace_id: str | None = None
    output_revision_id: str | None = None
    error_category: str | None = None


def record_shadow_parity(conn: sqlite3.Connection, item: ShadowParityRecord) -> str:
    """Persist hashes/counts and governed identifiers, never prompt or memory bodies."""
    telemetry_id = f"sp_{uuid.uuid4().hex}"
    _insert_shadow_parity(conn, telemetry_id, item)
    return telemetry_id


def record_shadow_parity_fail_open(item: ShadowParityRecord) -> str | None:
    """Write telemetry independently; a telemetry outage never changes request output."""
    from src.learning.runtime import learning_writes_disabled

    if learning_writes_disabled():
        return None
    try:
        from src.db.connection import scoped_observability_write
        telemetry_id = f"sp_{uuid.uuid4().hex}"
        with scoped_observability_write() as conn:
            _insert_shadow_parity(conn, telemetry_id, item)
        return telemetry_id
    except Exception:
        return None


def _insert_shadow_parity(conn: sqlite3.Connection, telemetry_id: str,
                          item: ShadowParityRecord) -> None:
    changed = item.legacy_text != item.v2_text
    omission = _omission(item.legacy_text, item.v2_text, item.error_category)
    conn.execute(
        """INSERT INTO learning_shadow_parity
           (telemetry_id,surface,operator_id,project_id,cohort,policy_version,
            trace_id,output_revision_id,legacy_hash,v2_hash,legacy_chars,v2_chars,
            changed_classification,selected_facts_json,error_category,created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))""",
        (telemetry_id, item.surface, item.operator_id, item.project_id, item.cohort,
         item.policy_version, item.trace_id, item.output_revision_id,
         _hash(item.legacy_text), _hash(item.v2_text), len(item.legacy_text), len(item.v2_text),
         "changed" if changed else "same",
         json.dumps([{"fact_id": fact_id, "version": version}
                     for fact_id, version in item.fact_versions], sort_keys=True), omission),
    )


def aggregate_shadow_parity(conn: sqlite3.Connection, *, surface: str | None = None) -> dict:
    where, args = (" WHERE surface=?", (surface,)) if surface else ("", ())
    rows = conn.execute(
        "SELECT changed_classification,error_category,COUNT(*) AS count "
        f"FROM learning_shadow_parity{where} GROUP BY changed_classification,error_category",
        args,
    ).fetchall()
    total = sum(int(row["count"]) for row in rows)
    changed = sum(int(row["count"]) for row in rows if row["changed_classification"] == "changed")
    errors = sum(int(row["count"]) for row in rows if row["error_category"])
    categories = {str(row["error_category"]): int(row["count"]) for row in rows if row["error_category"]}
    return {
        "surface": surface, "total": total, "changed": changed, "errors": errors,
        "mismatch_rate": changed / total if total else 0.0,
        "error_rate": errors / total if total else 0.0, "error_categories": categories,
    }


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _omission(legacy: str, v2: str, error: str | None) -> str | None:
    if error:
        return error
    if legacy.strip() and not v2.strip():
        return "v2_empty"
    if v2.strip() and not legacy.strip():
        return "legacy_empty"
    return None
