"""Governed rollback queue and outside-serializer delivery worker."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from src.db.connection import scoped_read, scoped_write
from src.learning.operation_service import transition_operation

TARGETS = ("compatibility_projection", "wiki_authority", "wiki_index", "context_cache")
MAX_ATTEMPTS = 5


def create_rollback_work(conn: sqlite3.Connection, *, operation_id: str, fact_id: str,
                         projection_event_id: str | None) -> None:
    snapshot = _ownership_snapshot(conn, fact_id)
    snapshot_json = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
    for target in TARGETS:
        conn.execute("""INSERT OR IGNORE INTO memory_rollback_targets
          (operation_id,fact_id,target,status,attempts,projection_event_id,ownership_snapshot_json)
          VALUES (?,?,?,'pending',0,?,?)""", (operation_id, fact_id, target, projection_event_id, snapshot_json))


def _ownership_snapshot(conn: sqlite3.Connection, fact_id: str) -> list[dict]:
    try:
        publication = conn.execute("""SELECT publication_id,canonical_path,owned_content_hash,status
          FROM wiki_publications WHERE fact_id=?""", (fact_id,)).fetchone()
    except sqlite3.OperationalError:
        publication = None
    if publication:
        return [{"publication_id": publication["publication_id"], "canonical_path": publication["canonical_path"],
                 "owned_content_hash": publication["owned_content_hash"], "publication_status": publication["status"]}]
    rows = conn.execute("""SELECT o.legacy_key,o.snapshot_json,r.kb_file,r.written_to_kb,r.kb_written_at
      FROM memory_projection_ownership o LEFT JOIN learned_rules r ON r.rule_id=o.legacy_key
      WHERE o.fact_id=? AND o.target='learned_rules' ORDER BY o.legacy_key""", (fact_id,)).fetchall()
    snapshot = []
    for row in rows:
        owned = json.loads(row["snapshot_json"] or "{}")
        entry = owned.get("wiki_entry")
        snapshot.append({"legacy_rule_id": row["legacy_key"], "kb_file": row["kb_file"] or owned.get("kb_file"),
            "written_to_kb": bool(row["written_to_kb"]), "kb_written_at": row["kb_written_at"],
            "wiki_entry": entry, "wiki_entry_hash": owned.get("wiki_entry_hash")})
    return snapshot


def _safe_wiki_path(root: Path, kb_file: str) -> Path | None:
    candidate = (root / kb_file).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


def process_rollback_work(*, max_targets: int = 50, at: datetime | None = None) -> int:
    """Process bounded pending work for the scheduler, never for a fact API action."""
    return _process_selected(max_targets=max_targets, at=at)


def retry_rollback_operation(*, operation_id: str, fact_id: str, actor_id: str,
                             reason: str, idempotency_key: str,
                             at: datetime | None = None) -> dict:
    """Retry only one authorized fact/operation rollback and return persisted state."""
    now = _utc(at or datetime.now(timezone.utc))
    from src.learning.control_authorization import authorize, resolve_rollback
    with scoped_write() as conn:
        authorize(resolve_rollback(conn, operation_id, fact_id), operator_id=actor_id)
        marker = f"rollback-retry:{idempotency_key}"
        prior = conn.execute(
            "SELECT metadata_json FROM activity_log WHERE action_type='learning_rollback_retry' AND object_ref=?",
            (marker,),
        ).fetchone()
        if prior:
            return json.loads(prior[0])["result"]
    _process_selected(operation_id=operation_id, fact_id=fact_id, max_targets=len(TARGETS), at=now)
    with scoped_write() as conn:
        authorize(resolve_rollback(conn, operation_id, fact_id), operator_id=actor_id)
        operation = conn.execute(
            "SELECT status,result_json FROM memory_lifecycle_operations WHERE operation_id=?", (operation_id,),
        ).fetchone()
        targets = [dict(row) for row in conn.execute(
            "SELECT target,status,attempts,last_error,completed_at FROM memory_rollback_targets WHERE operation_id=? AND fact_id=? ORDER BY target",
            (operation_id, fact_id),
        ).fetchall()]
        result = {"operation_id": operation_id, "fact_id": fact_id,
                  "status": operation["status"], "targets": targets}
        from src.repositories.activity_log_repo import insert_activity
        insert_activity(conn, actor=actor_id, surface="learning_governance",
            action_type="learning_rollback_retry", object_ref=marker,
            summary=f"Retried rollback {operation_id} for {fact_id}",
            metadata_json=json.dumps({"reason": reason, "result": result}, sort_keys=True))
        return result


def _process_selected(*, max_targets: int, at: datetime | None = None,
                      operation_id: str | None = None, fact_id: str | None = None) -> int:
    now = _utc(at or datetime.now(timezone.utc))
    clauses = ["status IN ('pending','partial_failed')"]
    params: list = []
    if operation_id is not None:
        clauses.append("operation_id=?"); params.append(operation_id)
    if fact_id is not None:
        clauses.append("fact_id=?"); params.append(fact_id)
    params.append(max_targets)
    with scoped_read() as conn:
        rows = conn.execute(f"""SELECT operation_id,fact_id,target,attempts,projection_event_id,ownership_snapshot_json
          FROM memory_rollback_targets WHERE {' AND '.join(clauses)}
          ORDER BY operation_id,fact_id,
          CASE target WHEN 'wiki_authority' THEN 0 WHEN 'compatibility_projection' THEN 1
                      WHEN 'wiki_index' THEN 2 ELSE 3 END LIMIT ?""", params).fetchall()
    processed = 0
    for row in rows:
        try:
            completed, error = _deliver(dict(row))
        except Exception as exc:
            completed, error = False, str(exc)
        _record_attempt(dict(row), completed=completed, error=error, at=now)
        processed += 1
    with scoped_write() as conn:
        _recompute_operations(conn, at=now)
    return processed


def sync_projection_rollbacks(conn: sqlite3.Connection, *, at: datetime) -> None:
    """Compatibility helper: project delivery state into rollback target state."""
    rows = conn.execute("""SELECT r.operation_id,r.fact_id,r.projection_event_id,
      d.status,d.attempts,d.last_error,d.completed_at FROM memory_rollback_targets r
      LEFT JOIN memory_projection_deliveries d ON d.event_id=r.projection_event_id
      WHERE r.target='compatibility_projection' AND r.status IN ('pending','partial_failed')""").fetchall()
    for row in rows:
        if row["status"] == "completed":
            conn.execute("""UPDATE memory_rollback_targets SET status='completed',attempts=?,last_error=NULL,completed_at=?
              WHERE operation_id=? AND fact_id=? AND target='compatibility_projection'""",
              (row["attempts"] or 0, row["completed_at"] or at.isoformat(), row["operation_id"], row["fact_id"]))
        elif row["status"] == "dead_letter":
            conn.execute("""UPDATE memory_rollback_targets SET status='dead_letter',attempts=?,last_error=?
              WHERE operation_id=? AND fact_id=? AND target='compatibility_projection'""",
              (row["attempts"] or 0, row["last_error"], row["operation_id"], row["fact_id"]))
    _recompute_operations(conn, at=at)


def _deliver(row: dict) -> tuple[bool, str | None]:
    target = row["target"]
    if target == "compatibility_projection":
        with scoped_read() as conn:
            statuses = [r[0] for r in conn.execute(
                "SELECT status FROM memory_projection_deliveries WHERE event_id=?", (row["projection_event_id"],)).fetchall()]
        if statuses and all(item == "completed" for item in statuses):
            return True, None
        if "dead_letter" in statuses:
            return False, "compatibility projection dead-lettered"
        return False, "compatibility projection remains pending"
    if target == "wiki_authority":
        return _rollback_wiki_authority(row)
    if target in {"wiki_index", "context_cache"}:
        with scoped_read() as conn:
            authority = conn.execute("""SELECT status FROM memory_rollback_targets
              WHERE operation_id=? AND fact_id=? AND target='wiki_authority'""",
              (row["operation_id"], row["fact_id"])).fetchone()
        if not authority or authority[0] != "completed":
            return False, "wiki authority rollback not completed"
    if target == "wiki_index":
        from src.wiki.domain_index import invalidate_wiki_domain_index
        from src.wiki.graph import invalidate_wiki_graph
        from src.wiki.indexer import invalidate_wiki_index
        invalidate_wiki_index(); invalidate_wiki_graph(); invalidate_wiki_domain_index()
        return True, None
    if target == "context_cache":
        from src.context.knowledge_loader import _load_knowledge_file
        from src.context.engine import _load_system_prompt
        from src.context.skills_index import build_skills_block
        _load_knowledge_file.cache_clear(); _load_system_prompt.cache_clear(); build_skills_block.cache_clear()
        try:
            from src.context.knowledge_index import invalidate_knowledge_index
            invalidate_knowledge_index()
        except (ImportError, AttributeError):
            pass
        return True, None
    raise ValueError(f"unknown rollback target: {target}")


def _rollback_wiki_authority(row: dict) -> tuple[bool, str | None]:
    """Remove only immutable, byte-verified Wiki entries captured at enqueue time."""
    snapshot = json.loads(row.get("ownership_snapshot_json") or "[]")
    if not snapshot:
        return False, "manual review required: no proven fact-owned Wiki authority snapshot"
    if snapshot[0].get("publication_id"):
        from config.settings import BASE_DIR
        from src.wiki.publication.rollback import rollback_publication
        result = rollback_publication(snapshot[0]["publication_id"], wiki_dir=BASE_DIR / "wiki",
                                      actor_id="lifecycle", reason="governed lifecycle rollback")
        if result.status == "revoked":
            return True, None
        return False, f"manual review required: {result.last_error or 'publication rollback ambiguous'}"
    from config import defaults
    if defaults.WIKI_PUBLICATION_V2 and not defaults.WIKI_PUBLICATION_DISABLED:
        return False, "manual review required: legacy Wiki ownership is not authoritative under publication v2"
    from src.learning.confidence_lifecycle import KB_DIR
    updates: list[tuple[Path, str]] = []
    for item in snapshot:
        rule_id = item.get("legacy_rule_id")
        kb_file = item.get("kb_file")
        entry = item.get("wiki_entry")
        entry_hash = item.get("wiki_entry_hash")
        if not rule_id or not kb_file or not entry or hashlib.sha256(entry.encode()).hexdigest() != entry_hash:
            return False, "manual review required: incomplete Wiki ownership snapshot"
        path = _safe_wiki_path(KB_DIR, kb_file)
        if not path or not path.exists():
            return False, "manual review required: owned Wiki file missing"
        content = path.read_text(encoding="utf-8")
        if content.count(entry) != 1:
            return False, "manual review required: exact owned Wiki entry changed or ambiguous"
        updated = content.replace(entry, "", 1)
        if f"**[{rule_id}]**" in updated:
            return False, "manual review required: duplicate owned Wiki marker remains"
        updates.append((path, updated))
    for path, updated in updates:
        path.write_text(updated, encoding="utf-8")
    with scoped_write() as conn:
        from src.learning.legacy_retirement import authorized_compatibility_projection
        with authorized_compatibility_projection("learned_rules"):
            for item in snapshot:
                conn.execute("""UPDATE learned_rules SET written_to_kb=0,kb_file=NULL,kb_written_at=NULL
                  WHERE rule_id=?""", (item["legacy_rule_id"],))
    return True, None


def _record_attempt(row: dict, *, completed: bool, error: str | None, at: datetime) -> None:
    attempt = row["attempts"] + 1
    manual_review = bool(error and "manual review required" in error)
    status = "completed" if completed else ("manual_review" if manual_review else ("dead_letter" if attempt >= MAX_ATTEMPTS else "partial_failed"))
    attempt_status = "completed" if completed else ("dead_letter" if attempt >= MAX_ATTEMPTS and not manual_review else "partial_failed")
    attempt_id = "mra_" + hashlib.sha256(f"{row['operation_id']}|{row['fact_id']}|{row['target']}|{attempt}".encode()).hexdigest()[:24]
    with scoped_write() as conn:
        current = conn.execute("SELECT status,attempts FROM memory_rollback_targets WHERE operation_id=? AND fact_id=? AND target=?",
                               (row["operation_id"], row["fact_id"], row["target"])).fetchone()
        if not current or current["status"] == "completed" or current["attempts"] >= attempt:
            return
        conn.execute("""INSERT INTO memory_rollback_attempts
          (attempt_id,operation_id,fact_id,target,attempt_number,status,error,started_at,completed_at)
          VALUES (?,?,?,?,?,?,?,?,?)""", (attempt_id, row["operation_id"], row["fact_id"], row["target"], attempt,
          attempt_status, error, at.isoformat(), at.isoformat()))
        conn.execute("""UPDATE memory_rollback_targets SET status=?,attempts=?,last_error=?,completed_at=?
          WHERE operation_id=? AND fact_id=? AND target=?""", (status, attempt, error,
          at.isoformat() if completed else None, row["operation_id"], row["fact_id"], row["target"]))


def _recompute_operations(conn: sqlite3.Connection, *, at: datetime) -> None:
    operations = conn.execute("SELECT operation_id,deadline,status FROM memory_lifecycle_operations WHERE status!='completed'").fetchall()
    for operation in operations:
        statuses = [row[0] for row in conn.execute("SELECT status FROM memory_rollback_targets WHERE operation_id=?", (operation["operation_id"],)).fetchall()]
        if statuses and all(status == "completed" for status in statuses):
            transition_operation(conn, operation_id=operation["operation_id"], status="completed", reason="all rollback targets completed", at=at)
        elif any(status in {"dead_letter", "manual_review"} for status in statuses):
            transition_operation(conn, operation_id=operation["operation_id"], status="dead_letter", reason="rollback target requires manual review or dead-lettered", at=at)
        elif at.isoformat() > operation["deadline"]:
            transition_operation(conn, operation_id=operation["operation_id"], status="slo_breached", reason="rollback deadline exceeded", at=at)
        elif any(status == "partial_failed" for status in statuses):
            transition_operation(conn, operation_id=operation["operation_id"], status="partial_failed", reason="rollback target attempt failed", at=at)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("rollback time must be timezone-aware")
    return value.astimezone(timezone.utc)
