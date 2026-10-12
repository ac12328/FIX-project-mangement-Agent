"""Persisted deterministic rollout policy for governed learning surfaces."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from enum import Enum

from config import defaults


class RolloutMode(str, Enum):
    LEGACY = "legacy"
    SHADOW = "shadow"
    V2 = "v2"


SURFACE_ORDER = ("chat", "web", "draft", "advisory", "wiki")
SUPPORTED_SURFACES = SURFACE_ORDER + ("tool",)


class StaleRolloutPolicy(ValueError):
    """Raised when a caller writes against an obsolete policy version."""


class RolloutOrderViolation(ValueError):
    """Raised when a cohort is promoted out of the approved surface order."""


class RolloutGateError(ValueError):
    """Raised when persisted release/retirement evidence is incomplete."""


@dataclass(frozen=True)
class EffectiveRollout:
    surface: str
    mode: RolloutMode
    cohort: str
    policy_version: int | None
    policy_id: str | None
    reason: str


def _policy_id(surface: str, cohort_type: str, cohort_id: str) -> str:
    raw = f"{surface}\0{cohort_type}\0{cohort_id}"
    return "lrp_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def set_rollout_policy(
    conn: sqlite3.Connection, *, surface: str, mode: RolloutMode | str,
    actor: str, reason: str, operator_id: str | None = None,
    project_id: str | None = None, expected_version: int | None = None,
) -> int:
    """Create or version one allowlisted cohort policy with optimistic locking."""
    if surface not in SUPPORTED_SURFACES:
        raise ValueError(f"unsupported rollout surface: {surface}")
    mode = RolloutMode(mode)
    if not actor.strip() or not reason.strip():
        raise ValueError("actor and reason are required")
    cohort_type, cohort_id = _cohort(operator_id, project_id)
    policy_id = _policy_id(surface, cohort_type, cohort_id)
    row = conn.execute(
        "SELECT version,mode FROM learning_rollout_policies WHERE policy_id=?", (policy_id,),
    ).fetchone()
    current_version = int(row["version"]) if row else 0
    if expected_version is not None and expected_version != current_version:
        raise StaleRolloutPolicy(
            f"stale rollout write: expected {expected_version}, current {current_version}"
        )
    if mode is RolloutMode.V2:
        _enforce_release_gates(conn)
        _enforce_promotion_order(conn, surface, operator_id, project_id)
    version = current_version + 1
    conn.execute(
        """INSERT INTO learning_rollout_policies
           (policy_id,surface,cohort_type,cohort_id,operator_id,project_id,mode,version,
            actor,reason,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now'))
           ON CONFLICT(policy_id) DO UPDATE SET mode=excluded.mode,version=excluded.version,
             actor=excluded.actor,reason=excluded.reason,updated_at=excluded.updated_at""",
        (policy_id, surface, cohort_type, cohort_id, operator_id, project_id,
         mode.value, version, actor, reason),
    )
    conn.execute(
        """INSERT INTO learning_rollout_policy_events
           (event_id,policy_id,surface,cohort_type,cohort_id,operator_id,project_id,
            old_mode,new_mode,policy_version,actor,reason,created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))""",
        (f"lrpe_{hashlib.sha256(f'{policy_id}:{version}'.encode()).hexdigest()[:24]}",
         policy_id, surface, cohort_type, cohort_id, operator_id, project_id,
         row["mode"] if row else None, mode.value, version, actor, reason),
    )
    return version


def rollback_cohort(
    conn: sqlite3.Connection, *, surface: str, actor: str, reason: str,
    operator_id: str | None = None, project_id: str | None = None,
    expected_version: int | None = None,
) -> int:
    """Select legacy and atomically cascade every later ordered surface."""
    version = set_rollout_policy(
        conn, surface=surface, mode=RolloutMode.LEGACY, actor=actor, reason=reason,
        operator_id=operator_id, project_id=project_id, expected_version=expected_version,
    )
    if surface in SURFACE_ORDER:
        cohort_type, cohort_id = _cohort(operator_id, project_id)
        for later in SURFACE_ORDER[SURFACE_ORDER.index(surface) + 1:]:
            row = conn.execute(
                """SELECT version,mode FROM learning_rollout_policies
                   WHERE surface=? AND cohort_type=? AND cohort_id=?""",
                (later, cohort_type, cohort_id),
            ).fetchone()
            if row and row["mode"] != RolloutMode.LEGACY.value:
                set_rollout_policy(
                    conn, surface=later, mode=RolloutMode.LEGACY, actor=actor,
                    reason=f"cascade from {surface}: {reason}", operator_id=operator_id,
                    project_id=project_id, expected_version=int(row["version"]),
                )
    return version


def resolve_effective_mode(
    conn: sqlite3.Connection, *, surface: str, operator_id: str | None,
    project_id: str | None,
) -> EffectiveRollout:
    """Resolve one mode; global emergency and surface flags are fail-closed ceilings."""
    if surface not in SUPPORTED_SURFACES:
        raise ValueError(f"unsupported rollout surface: {surface}")
    if defaults.MEMORY_RETRIEVAL_DISABLED:
        return EffectiveRollout(surface, RolloutMode.LEGACY, "emergency", None, None,
                                "global retrieval control")
    if defaults.MEMORY_RETRIEVER_SHADOW:
        return EffectiveRollout(surface, RolloutMode.SHADOW, "global-shadow", None, None,
                                "global shadow control")
    row = _matching_policy(conn, surface, operator_id, project_id)
    default_mode = RolloutMode.V2 if _surface_enabled(surface) else RolloutMode.LEGACY
    if row:
        requested = RolloutMode(row["mode"])
        # A disabled surface can still compare safely in shadow, but can never serve v2.
        mode = RolloutMode.LEGACY if requested is RolloutMode.V2 and not _surface_enabled(surface) else requested
        return EffectiveRollout(
            surface, mode, f'{row["cohort_type"]}:{row["cohort_id"]}',
            int(row["version"]), row["policy_id"], row["reason"],
        )
    return EffectiveRollout(surface, default_mode, "default", None, None, "runtime defaults")


def _matching_policy(conn, surface, operator_id, project_id):
    candidates = []
    if operator_id and project_id:
        candidates.append(("operator_project", f"{operator_id}|{project_id}"))
    if project_id:
        candidates.append(("project", project_id))
    if operator_id:
        candidates.append(("operator", operator_id))
    candidates.append(("surface", "*"))
    for cohort_type, cohort_id in candidates:
        row = conn.execute(
            """SELECT * FROM learning_rollout_policies
               WHERE surface=? AND cohort_type=? AND cohort_id=?""",
            (surface, cohort_type, cohort_id),
        ).fetchone()
        if row:
            return row
    return None


def _cohort(operator_id, project_id):
    if operator_id and project_id:
        return "operator_project", f"{operator_id}|{project_id}"
    if project_id:
        return "project", project_id
    if operator_id:
        return "operator", operator_id
    return "surface", "*"


def _surface_enabled(surface: str) -> bool:
    # Tool remains separately governed but conservatively uses its existing explicit flag.
    if surface == "wiki":
        return bool(defaults.MEMORY_RETRIEVAL_V2_ADVISORY)
    return bool(getattr(defaults, f"MEMORY_RETRIEVAL_V2_{surface.upper()}"))


def _enforce_promotion_order(conn, surface, operator_id, project_id):
    if surface == "tool":
        return
    cohort_type, cohort_id = _cohort(operator_id, project_id)
    index = SURFACE_ORDER.index(surface)
    for prerequisite in SURFACE_ORDER[:index]:
        row = conn.execute(
            """SELECT mode FROM learning_rollout_policies
               WHERE surface=? AND cohort_type=? AND cohort_id=?""",
            (prerequisite, cohort_type, cohort_id),
        ).fetchone()
        if not row or row["mode"] != RolloutMode.V2.value:
            raise RolloutOrderViolation(
                f"{surface} requires {prerequisite} in v2 for the exact same cohort"
            )


def _enforce_release_gates(conn):
    from src.learning.legacy_retirement import (
        RetirementMode, get_retirement_state, retirement_gate_status,
    )
    gates = retirement_gate_status(conn)
    missing = [name for name, gate in gates.items() if not gate["passed"]]
    if missing:
        raise RolloutGateError("rollout gates not satisfied: " + ", ".join(missing))
    if get_retirement_state(conn).mode is RetirementMode.COMPATIBILITY:
        raise RolloutGateError("legacy retirement must be persisted as read_only or retired")
