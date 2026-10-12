"""Audited retirement boundary for legacy learning compatibility stores."""
from __future__ import annotations

import hashlib
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
from typing import Iterator


class RetirementMode(str, Enum):
    COMPATIBILITY = "compatibility"
    READ_ONLY = "read_only"
    RETIRED = "retired"


class LegacyMutationBlocked(PermissionError):
    """Raised when an independent legacy learning mutation is not permitted."""


class StaleRetirementState(ValueError):
    """Raised for an optimistic-lock version mismatch."""


class RetirementGateError(ValueError):
    """Raised when release evidence is incomplete."""


REQUIRED_GATES = (
    "backfill_complete",
    "parity_acceptable",
    "canary_stable",
    "stable_cycle_recorded",
)
_STORES = frozenset({
    "agent_notes", "operator_memory", "learned_rules", "response_pattern_memory",
})
_projection_scope: ContextVar[str | None] = ContextVar("legacy_projection_scope", default=None)


@dataclass(frozen=True)
class RetirementState:
    mode: RetirementMode
    version: int
    actor: str
    reason: str
    updated_at: str


def get_retirement_state(conn: sqlite3.Connection) -> RetirementState:
    row = conn.execute(
        "SELECT mode,version,actor,reason,updated_at FROM learning_legacy_retirement WHERE singleton_id=1"
    ).fetchone()
    if not row:
        raise RuntimeError("legacy retirement state is not initialized")
    values = dict(row) if isinstance(row, sqlite3.Row) else {
        "mode": row[0], "version": row[1], "actor": row[2],
        "reason": row[3], "updated_at": row[4],
    }
    return RetirementState(
        RetirementMode(values["mode"]), int(values["version"]),
        values["actor"], values["reason"], values["updated_at"],
    )


def record_retirement_gate(
    conn: sqlite3.Connection, *, gate: str, passed: bool, evidence_ref: str,
    actor: str, reason: str,
) -> None:
    """Persist a checkable release gate and its non-secret evidence reference."""
    if gate not in REQUIRED_GATES:
        raise ValueError(f"unsupported retirement gate: {gate}")
    _require_actor_reason(actor, reason)
    if not evidence_ref.strip():
        raise ValueError("evidence_ref is required")
    conn.execute(
        """INSERT INTO learning_legacy_retirement_gates
           (gate_name,passed,evidence_ref,actor,reason,recorded_at)
           VALUES (?,?,?,?,?,datetime('now'))
           ON CONFLICT(gate_name) DO UPDATE SET passed=excluded.passed,
             evidence_ref=excluded.evidence_ref,actor=excluded.actor,
             reason=excluded.reason,recorded_at=excluded.recorded_at""",
        (gate, int(passed), evidence_ref.strip(), actor.strip(), reason.strip()),
    )


def retirement_gate_status(conn: sqlite3.Connection) -> dict[str, dict]:
    rows = conn.execute(
        "SELECT gate_name,passed,evidence_ref,actor,reason,recorded_at FROM learning_legacy_retirement_gates"
    ).fetchall()
    columns = ("gate_name", "passed", "evidence_ref", "actor", "reason", "recorded_at")
    found = {
        (row["gate_name"] if isinstance(row, sqlite3.Row) else row[0]):
        (dict(row) if isinstance(row, sqlite3.Row) else dict(zip(columns, row)))
        for row in rows
    }
    return {
        gate: found.get(gate, {"gate_name": gate, "passed": 0, "evidence_ref": None,
                               "actor": None, "reason": None, "recorded_at": None})
        for gate in REQUIRED_GATES
    }


def set_retirement_mode(
    conn: sqlite3.Connection, *, mode: RetirementMode | str, actor: str,
    reason: str, expected_version: int,
) -> RetirementState:
    """Advance only with release evidence; rollback is immediate and audited."""
    _require_actor_reason(actor, reason)
    requested = RetirementMode(mode)
    current = get_retirement_state(conn)
    if expected_version != current.version:
        raise StaleRetirementState(
            f"stale retirement write: expected {expected_version}, current {current.version}"
        )
    order = {RetirementMode.COMPATIBILITY: 0, RetirementMode.READ_ONLY: 1, RetirementMode.RETIRED: 2}
    if order[requested] > order[current.mode]:
        missing = [name for name, gate in retirement_gate_status(conn).items() if not gate["passed"]]
        if missing:
            raise RetirementGateError("retirement gates not satisfied: " + ", ".join(missing))
        if order[requested] - order[current.mode] != 1:
            raise RetirementGateError("retirement modes must advance one step at a time")
    version = current.version + 1
    conn.execute(
        """UPDATE learning_legacy_retirement
           SET mode=?,version=?,actor=?,reason=?,updated_at=datetime('now')
           WHERE singleton_id=1 AND version=?""",
        (requested.value, version, actor.strip(), reason.strip(), current.version),
    )
    if conn.execute("SELECT changes()").fetchone()[0] != 1:
        raise StaleRetirementState("retirement state changed concurrently")
    event_id = "lrte_" + hashlib.sha256(
        f"{version}\0{current.mode.value}\0{requested.value}\0{actor}\0{reason}".encode()
    ).hexdigest()[:24]
    conn.execute(
        """INSERT INTO learning_legacy_retirement_events
           (event_id,old_mode,new_mode,state_version,actor,reason,created_at)
           VALUES (?,?,?,?,?,?,datetime('now'))""",
        (event_id, current.mode.value, requested.value, version, actor.strip(), reason.strip()),
    )
    return get_retirement_state(conn)


@contextmanager
def authorized_compatibility_projection(target: str) -> Iterator[None]:
    """Authorize only governed canonical projection or rollback delivery."""
    if target not in _STORES:
        raise ValueError(f"unsupported projection target: {target}")
    token = _projection_scope.set(target)
    try:
        yield
    finally:
        _projection_scope.reset(token)


def require_legacy_mutation(conn: sqlite3.Connection, store: str) -> None:
    """Central guard for every legacy learning mutation entry point."""
    if store not in _STORES:
        raise ValueError(f"unsupported legacy learning store: {store}")
    if _projection_scope.get() == store:
        return
    mode = get_retirement_state(conn).mode
    if mode is not RetirementMode.COMPATIBILITY:
        raise LegacyMutationBlocked(f"independent {store} mutations are blocked in {mode.value} mode")


def legacy_injection_allowed(
    conn: sqlite3.Connection, store: str, *, safety_critical: bool = False,
) -> bool:
    """Resolve legacy prompt injection without changing compatibility/API reads."""
    if store not in _STORES:
        raise ValueError(f"unsupported legacy learning store: {store}")
    if safety_critical:
        return True
    return get_retirement_state(conn).mode is not RetirementMode.RETIRED


def written_to_kb_authoritative(conn: sqlite3.Connection) -> bool:
    return get_retirement_state(conn).mode is not RetirementMode.RETIRED


def _require_actor_reason(actor: str, reason: str) -> None:
    if not actor.strip() or not reason.strip():
        raise ValueError("actor and reason are required")
