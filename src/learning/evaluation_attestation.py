"""Resolve persisted evaluation attestations for governed control-plane actions."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from src.db.connection import scoped_read


class PersistedEvaluationEngine(BaseModel):
    """Replay-engine identity verified against the latest persisted evaluation."""

    model_config = ConfigDict(frozen=True)
    attested_replay_hash: str
    attested_runtime_hash: str


def persisted_evaluation_engine(candidate_id: str, candidate_version: int) -> PersistedEvaluationEngine:
    with scoped_read() as conn:
        row = conn.execute(
            """SELECT replay_hash,runtime_hash FROM candidate_evaluations
               WHERE candidate_id=? AND candidate_version=?
               ORDER BY evaluated_at DESC,created_at DESC,evaluation_id DESC LIMIT 1""",
            (candidate_id, candidate_version),
        ).fetchone()
    if row is None:
        raise ValueError("replacement candidate requires a persisted evaluation")
    return PersistedEvaluationEngine(
        attested_replay_hash=row["replay_hash"],
        attested_runtime_hash=row["runtime_hash"],
    )
