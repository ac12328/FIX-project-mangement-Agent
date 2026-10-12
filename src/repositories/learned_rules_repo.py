"""
Learned rules repository.

Canonical statuses (injectable only when approved):
  approved | pending_review | rejected | archived

Legacy status ``active`` is NOT injectable. Migration normalizes it to
approved (confidence >= 0.6) or pending_review.
"""

import json
import sqlite3

# Statuses that may appear in prompts via get_applicable_rules
INJECTABLE_STATUS = "approved"
CANONICAL_STATUSES = frozenset({
    "approved",
    "pending_review",
    "rejected",
    "archived",
})


def normalize_legacy_active_statuses(conn: sqlite3.Connection) -> int:
    """Map legacy status=active → approved (conf>=0.6) or pending_review. Returns rows updated."""
    from src.learning.legacy_retirement import require_legacy_mutation
    require_legacy_mutation(conn, "learned_rules")
    cur = conn.execute(
        """UPDATE learned_rules
           SET status = CASE
             WHEN COALESCE(confidence, 0) >= 0.6 THEN 'approved'
             ELSE 'pending_review'
           END
           WHERE status = 'active'"""
    )
    return cur.rowcount


def get_applicable_rules(
    conn: sqlite3.Connection,
    *,
    market: str | None = None,
    limit: int = 15,
    memory_request=None,
) -> list[dict]:
    """Return high-confidence approved rules, optionally filtered by market.

    Only ``status = 'approved'`` with ``confidence >= 0.6`` are injectable.
    Legacy ``active`` and ``pending_review`` never appear here.

    Checks ``applicable_to`` JSON array for ``"*"`` (global) or a market code match.
    Results are ordered by usage_count DESC, confidence DESC.
    """
    from src.learning.legacy_retirement import legacy_injection_allowed
    if not legacy_injection_allowed(conn, "learned_rules"):
        return []
    rows = conn.execute(
        """SELECT rule_id, rule, condition, applicable_to, confidence, usage_count, status
           FROM learned_rules
           WHERE status = 'approved' AND confidence >= 0.6
           ORDER BY usage_count DESC, confidence DESC""",
    ).fetchall()

    # Projected facts need the same request-specific authority/scope/expiry
    # checks as canonical injection. No request means no projected authority.
    owners = {row["legacy_key"]: (row["fact_id"], row["fact_version"]) for row in conn.execute(
        "SELECT legacy_key,fact_id,fact_version FROM memory_projection_ownership WHERE target='learned_rules'")}
    authorized = set()
    if owners and memory_request is not None:
        from src.memory.retriever import MemoryRetriever
        authorized = MemoryRetriever().authorized_versions(conn, memory_request)
    result: list[dict] = []
    for row in rows:
        owner = owners.get(row["rule_id"])
        if owner is not None and owner not in authorized:
            continue
        d = dict(row)
        try:
            targets = json.loads(d.get("applicable_to") or '["*"]')
        except (json.JSONDecodeError, TypeError):
            targets = ["*"]
        if "*" in targets:
            result.append(d)
        elif market and market.upper() in (t.upper() for t in targets):
            result.append(d)
        if len(result) >= limit:
            break
    return result


def format_rules_for_prompt(rules: list[dict]) -> str:
    """Format learned rules as a compact string for LLM system prompt injection."""
    if not rules:
        return ""
    lines = []
    for r in rules:
        condition = r.get("condition") or ""
        rule_text = r.get("rule") or ""
        entry = f"- {rule_text}"
        if condition:
            entry = f"- IF {condition} THEN {rule_text}"
        lines.append(entry)
    return "Learned Rules (from past project experience):\n" + "\n".join(lines)


def insert_learned_rule(conn: sqlite3.Connection, rule_data: dict) -> None:
    """Insert a learned rule."""
    from src.learning.legacy_retirement import require_legacy_mutation
    require_legacy_mutation(conn, "learned_rules")
    conn.execute(
        """INSERT INTO learned_rules
           (rule_id, created_at, trigger_type, source_project_id,
            condition, rule, extra_knowledge, scope, applicable_to,
            status, conflict_with, usage_count, confidence,
            expanded_by, expanded_at, written_to_kb, kb_file, kb_written_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            rule_data["rule_id"],
            rule_data["created_at"],
            rule_data.get("trigger_type", "procedure"),
            rule_data.get("source_project_id"),
            rule_data.get("condition"),
            rule_data.get("rule"),
            rule_data.get("extra_knowledge", "{}"),
            rule_data.get("scope", "global"),
            rule_data.get("applicable_to", '["*"]'),
            rule_data.get("status", "pending_review"),
            rule_data.get("conflict_with", "[]"),
            rule_data.get("usage_count", 0),
            rule_data.get("confidence", 0.8),
            rule_data.get("expanded_by"),
            rule_data.get("expanded_at"),
            rule_data.get("written_to_kb", 0),
            rule_data.get("kb_file"),
            rule_data.get("kb_written_at"),
        ),
    )


def get_all_learned_rules(conn: sqlite3.Connection) -> list[dict]:
    """Get all learned rules."""
    rows = conn.execute("SELECT * FROM learned_rules ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def get_pending_rules(conn: sqlite3.Connection) -> list[dict]:
    """Get rules awaiting operator review."""
    rows = conn.execute(
        """SELECT rule_id, rule, condition, scope, applicable_to,
                  confidence, source_project_id, created_at
           FROM learned_rules
           WHERE status = 'pending_review'
           ORDER BY confidence DESC, created_at DESC""",
    ).fetchall()
    return [dict(r) for r in rows]


def approve_rule(conn: sqlite3.Connection, rule_id: str) -> bool:
    """Approve a pending rule so it becomes active in prompts."""
    from src.learning.legacy_retirement import require_legacy_mutation
    require_legacy_mutation(conn, "learned_rules")
    cur = conn.execute(
        "UPDATE learned_rules SET status = 'approved' WHERE rule_id = ? AND status = 'pending_review'",
        (rule_id,),
    )
    return cur.rowcount > 0


def reject_rule(conn: sqlite3.Connection, rule_id: str) -> bool:
    """Reject a pending rule — it will never be loaded into prompts."""
    from src.learning.legacy_retirement import require_legacy_mutation
    require_legacy_mutation(conn, "learned_rules")
    cur = conn.execute(
        "UPDATE learned_rules SET status = 'rejected' WHERE rule_id = ? AND status = 'pending_review'",
        (rule_id,),
    )
    return cur.rowcount > 0


def get_learned_rule(conn: sqlite3.Connection, rule_id: str) -> dict | None:
    """Get a single learned rule by ID."""
    row = conn.execute(
        "SELECT * FROM learned_rules WHERE rule_id = ?", (rule_id,)
    ).fetchone()
    return dict(row) if row else None


def increment_usage_count(conn: sqlite3.Connection, rule_id: str) -> bool:
    """Increment usage_count and touch updated_at for a learned rule."""
    from datetime import datetime, timezone
    from src.learning.legacy_retirement import require_legacy_mutation
    require_legacy_mutation(conn, "learned_rules")

    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """UPDATE learned_rules
           SET usage_count = usage_count + 1, updated_at = ?
           WHERE rule_id = ?""",
        (now, rule_id),
    )
    return cur.rowcount > 0


def find_similar_rule(
    conn: sqlite3.Connection,
    rule_text: str,
    *,
    threshold: float = 0.8,
    exclude_status: str = "rejected",
) -> dict | None:
    """Find an existing rule with similar text using SequenceMatcher.

    Returns the most similar rule dict if ratio >= threshold, else None.
    """
    import difflib
    import re

    def _normalize(text: str) -> str:
        text = (text or "").lower().strip()
        text = re.sub(r"\s+", " ", text)
        text = re.sub(r"[^\w\s]", "", text)
        return text

    normalized = _normalize(rule_text)
    if not normalized:
        return None

    rows = conn.execute(
        "SELECT rule_id, rule, confidence, usage_count, status FROM learned_rules WHERE status != ?",
        (exclude_status,),
    ).fetchall()

    best_match: dict | None = None
    best_ratio = 0.0
    for row in rows:
        d = dict(row)
        ratio = difflib.SequenceMatcher(None, normalized, _normalize(d["rule"])).ratio()
        if ratio >= threshold and ratio > best_ratio:
            best_ratio = ratio
            best_match = d
    return best_match


def merge_into_existing(
    conn: sqlite3.Connection,
    rule_id: str,
    new_confidence: float,
) -> None:
    """Merge a duplicate into an existing rule -- bump usage_count and confidence."""
    from datetime import datetime, timezone
    from src.learning.legacy_retirement import require_legacy_mutation
    require_legacy_mutation(conn, "learned_rules")

    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """UPDATE learned_rules
           SET usage_count = usage_count + 1,
               confidence = MAX(confidence, ?),
               updated_at = ?
           WHERE rule_id = ?""",
        (new_confidence, now, rule_id),
    )


def bulk_approve_rules(conn: sqlite3.Connection, rule_ids: list[str]) -> int:
    """Approve multiple pending rules. Returns count updated."""
    if not rule_ids:
        return 0
    count = 0
    for rid in rule_ids:
        if approve_rule(conn, rid):
            count += 1
    return count


def bulk_reject_rules(conn: sqlite3.Connection, rule_ids: list[str]) -> int:
    """Reject multiple pending rules. Returns count updated."""
    if not rule_ids:
        return 0
    count = 0
    for rid in rule_ids:
        if reject_rule(conn, rid):
            count += 1
    return count


def learning_sink_metrics(conn: sqlite3.Connection) -> dict:
    """Return counts for learning sink health dashboards."""
    def _count(sql: str, params: tuple = ()) -> int:
        row = conn.execute(sql, params).fetchone()
        if row is None:
            return 0
        return int(row[0] if not hasattr(row, "keys") else row[list(row.keys())[0]])

    metrics = {
        "approved_injectable": _count(
            "SELECT COUNT(*) FROM learned_rules WHERE status='approved' AND confidence >= 0.6"
        ),
        "pending": _count(
            "SELECT COUNT(*) FROM learned_rules WHERE status='pending_review'"
        ),
        "rejected": _count(
            "SELECT COUNT(*) FROM learned_rules WHERE status='rejected'"
        ),
        "archived": _count(
            "SELECT COUNT(*) FROM learned_rules WHERE status='archived'"
        ),
        "legacy_active": _count(
            "SELECT COUNT(*) FROM learned_rules WHERE status='active'"
        ),
        "usage_nonzero": _count(
            "SELECT COUNT(*) FROM learned_rules WHERE COALESCE(usage_count,0) > 0"
        ),
    }
    try:
        metrics["ai_feedback"] = _count("SELECT COUNT(*) FROM ai_feedback")
    except Exception:
        metrics["ai_feedback"] = 0
    try:
        metrics["response_patterns"] = _count(
            "SELECT COUNT(*) FROM response_pattern_memory"
        )
    except Exception:
        metrics["response_patterns"] = 0
    return metrics
