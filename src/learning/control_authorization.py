"""Authorization-first ownership resolution for learning governance controls."""
from __future__ import annotations

import sqlite3
from enum import Enum

from pydantic import BaseModel, ConfigDict


class ResourceKind(str, Enum):
    CANDIDATE = "candidate"
    FACT = "fact"
    CONFLICT = "conflict"
    JOB = "job"
    ROLLBACK = "rollback"


class ConflictAccessPolicy(str, Enum):
    SINGLE_OPERATOR = "single_operator"
    GOVERNANCE_ADMIN_REQUIRED = "governance_admin_required"


class Ownership(BaseModel):
    model_config = ConfigDict(frozen=True)
    kind: ResourceKind
    resource_id: str
    operator_ids: frozenset[str]
    candidate_versions: tuple[tuple[str, int], ...] = ()
    fact_ids: tuple[str, ...] = ()
    policy: ConflictAccessPolicy = ConflictAccessPolicy.SINGLE_OPERATOR


class ResourceNotFound(KeyError):
    pass


class GovernanceAdminRequired(PermissionError):
    pass


def resolve_candidate(conn: sqlite3.Connection, candidate_id: str, candidate_version: int) -> Ownership:
    row = conn.execute(
        """SELECT o.operator_id FROM memory_candidates c
           JOIN learning_observations o USING(observation_id)
           WHERE c.candidate_id=? AND c.candidate_version=?""",
        (candidate_id, candidate_version),
    ).fetchone()
    if not row:
        raise ResourceNotFound(candidate_id)
    return Ownership(kind=ResourceKind.CANDIDATE, resource_id=candidate_id,
                     operator_ids=frozenset({row[0]}), candidate_versions=((candidate_id, candidate_version),))


def resolve_fact(conn: sqlite3.Connection, fact_id: str) -> Ownership:
    row = conn.execute(
        """SELECT o.operator_id,f.candidate_id,f.candidate_version FROM memory_facts f
           JOIN memory_candidates c USING(candidate_id,candidate_version)
           JOIN learning_observations o USING(observation_id) WHERE f.fact_id=?""",
        (fact_id,),
    ).fetchone()
    if not row:
        raise ResourceNotFound(fact_id)
    return Ownership(kind=ResourceKind.FACT, resource_id=fact_id, operator_ids=frozenset({row[0]}),
                     candidate_versions=((row[1], row[2]),), fact_ids=(fact_id,))


def resolve_job(conn: sqlite3.Connection, job_id: str) -> Ownership:
    row = conn.execute(
        """SELECT o.operator_id FROM learning_outbox j
           JOIN learning_observations o USING(observation_id) WHERE j.job_id=?""",
        (job_id,),
    ).fetchone()
    if not row:
        raise ResourceNotFound(job_id)
    return Ownership(kind=ResourceKind.JOB, resource_id=job_id, operator_ids=frozenset({row[0]}))


def resolve_conflict(conn: sqlite3.Connection, conflict_id: str) -> Ownership:
    rows = conn.execute(
        """SELECT DISTINCT o.operator_id,m.candidate_id,m.candidate_version
           FROM memory_conflict_members m
           JOIN memory_candidates c USING(candidate_id,candidate_version)
           JOIN learning_observations o USING(observation_id)
           WHERE m.conflict_id=? ORDER BY o.operator_id,m.candidate_id,m.candidate_version""",
        (conflict_id,),
    ).fetchall()
    if not rows:
        raise ResourceNotFound(conflict_id)
    operators = frozenset(row[0] for row in rows)
    return Ownership(kind=ResourceKind.CONFLICT, resource_id=conflict_id, operator_ids=operators,
                     candidate_versions=tuple((row[1], row[2]) for row in rows),
                     policy=(ConflictAccessPolicy.SINGLE_OPERATOR if len(operators) == 1
                             else ConflictAccessPolicy.GOVERNANCE_ADMIN_REQUIRED))


def resolve_rollback(conn: sqlite3.Connection, operation_id: str, fact_id: str | None = None) -> Ownership:
    params: tuple = (operation_id,) if fact_id is None else (operation_id, fact_id)
    fact_clause = "" if fact_id is None else " AND r.fact_id=?"
    rows = conn.execute(
        """SELECT DISTINCT o.operator_id,r.fact_id,f.candidate_id,f.candidate_version
           FROM memory_rollback_targets r
           JOIN memory_facts f USING(fact_id)
           JOIN memory_candidates c USING(candidate_id,candidate_version)
           JOIN learning_observations o USING(observation_id)
           WHERE r.operation_id=?""" + fact_clause,
        params,
    ).fetchall()
    if not rows:
        raise ResourceNotFound(operation_id)
    operators = frozenset(row[0] for row in rows)
    return Ownership(kind=ResourceKind.ROLLBACK, resource_id=operation_id, operator_ids=operators,
                     fact_ids=tuple(sorted({row[1] for row in rows})),
                     candidate_versions=tuple(sorted({(row[2], row[3]) for row in rows})),
                     policy=(ConflictAccessPolicy.SINGLE_OPERATOR if len(operators) == 1
                             else ConflictAccessPolicy.GOVERNANCE_ADMIN_REQUIRED))


def authorize(ownership: Ownership, *, operator_id: str, actor_role: str = "operator") -> Ownership:
    if ownership.policy is ConflictAccessPolicy.GOVERNANCE_ADMIN_REQUIRED:
        if actor_role not in {"governance_admin", "security_admin"}:
            raise GovernanceAdminRequired("multi-operator governance resource requires an authorized governance admin")
        return ownership
    if ownership.operator_ids != frozenset({operator_id}):
        # Deliberately indistinguishable from an absent resource.
        raise ResourceNotFound(ownership.resource_id)
    return ownership
