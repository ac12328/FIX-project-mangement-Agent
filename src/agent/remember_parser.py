"""Hot-path parser: persist forbidden rules from operator remember phrases."""

from __future__ import annotations

import logging
import re
import sqlite3

from src.agent.intent import detect_project_id
from src.memory.reform_flags import operator_memory_enabled

logger = logging.getLogger(__name__)

_REMEMBER_NO_CLOSE_RE = re.compile(
    r"(?:記住|remember)"
    r".{0,60}"
    r"(?:唔好|不要|勿|don't|do not|never|not to)"
    r".{0,30}"
    r"(?:close|關閉|结案|結案|close\s+out)",
    re.IGNORECASE | re.DOTALL,
)


def try_persist_remember_forbidden(
    conn: sqlite3.Connection | None,
    user_text: str,
    *,
    operator_id: str | None = None,
) -> dict | None:
    """Write a forbidden close rule when operator asks to remember a ban."""
    if conn is None or not operator_memory_enabled() or not user_text:
        return None
    from src.learning.governance_flags import candidate_only, writes_disabled
    if candidate_only() or writes_disabled():
        logger.info("Hot-path remember blocked by learning governance controls")
        return None
    if not _REMEMBER_NO_CLOSE_RE.search(user_text):
        return None

    project_id = detect_project_id(user_text)
    if not project_id:
        return None

    from src.agent.operator_identity import resolve_operator_id
    from src.memory.operator_memory_store import OperatorMemoryStore

    op_id = operator_id or resolve_operator_id(conn)
    rule_key = f"no_close_{project_id.lower()}"
    store = OperatorMemoryStore(conn, op_id)
    store.put_forbidden(
        project_id=project_id,
        rule_key=rule_key,
        description=user_text.strip()[:500],
        blocked_fields={"project_status": ["Closed", "Archived", "Completed"]},
        source="operator",
    )
    conn.commit()
    logger.info("Hot-path remember: forbidden close for %s (%s)", project_id, rule_key)
    return {"project_id": project_id, "rule_key": rule_key}