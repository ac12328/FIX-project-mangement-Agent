"""Authorized conflict resolution and typed equivalence merge service."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from src.db.connection import scoped_write
from src.learning.control_authorization import authorize, resolve_conflict
from src.learning.governance.contradiction import transition_conflict
from src.learning.governance.dedup import claims_equivalent
from src.learning.governance.validator import load_candidate
from src.repositories.memory_conflict_repo import ConflictStatus, get_conflict


class ConflictControlResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    conflict_id: str
    status: str
    candidate_id: str | None = None
    candidate_version: int | None = None


def act(conflict_id: str, *, action: str, actor_id: str, actor_role: str, reason: str,
        candidate_id: str | None = None, candidate_version: int | None = None) -> ConflictControlResult:
    with scoped_write() as conn:
        authorize(resolve_conflict(conn, conflict_id), operator_id=actor_id, actor_role=actor_role)
        conflict = get_conflict(conn, conflict_id)
        if action == "resolve":
            transition_conflict(
                conn,
                conflict_id,
                status=ConflictStatus.REVIEW_RESOLVED,
                actor_id=actor_id,
                reason=reason,
                action=action,
            )
            return ConflictControlResult(conflict_id=conflict_id, status=ConflictStatus.REVIEW_RESOLVED.value)
        if not candidate_id or not candidate_version:
            raise ValueError(f"{action} requires the selected candidate and version")
        selected = next((item for item in conflict.members if item.candidate_id == candidate_id and item.candidate_version == candidate_version), None)
        if selected is None:
            raise ValueError("selected candidate must be a conflict member")
        if action == "merge":
            canonical = load_candidate(conn, candidate_id, candidate_version)
            if any(not claims_equivalent(canonical.predicate, canonical.proposed_value,
                                         load_candidate(conn, member.candidate_id, member.candidate_version).proposed_value)
                   for member in conflict.members):
                raise ValueError("conflict members are not typed-equivalent and cannot be merged")
        transition_conflict(
            conn,
            conflict_id,
            status=ConflictStatus.SUPERSEDED,
            actor_id=actor_id,
            superseded_by_candidate_id=candidate_id,
            superseded_by_version=candidate_version,
            reason=reason,
            action=action,
        )
        return ConflictControlResult(conflict_id=conflict_id, status=ConflictStatus.SUPERSEDED.value,
                                     candidate_id=candidate_id, candidate_version=candidate_version)
