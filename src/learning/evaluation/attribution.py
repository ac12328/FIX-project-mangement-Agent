"""Fail-closed exact outcome attribution to persisted memory exposures."""
from __future__ import annotations

import hashlib
import sqlite3
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.memory.exposure_repo import list_exposures

FeedbackScope = Literal["all_rendered", "style", "unrelated"]


class AttributionRequest(BaseModel):
    model_config = ConfigDict(frozen=True)
    idempotency_key: str
    operator_id: str
    surface: str
    trace_id: str
    output_revision_id: str
    outcome_type: str
    outcome_value: float
    actor_id: str
    reason: str | None = None
    feedback_id: str | None = None
    fact_id: str | None = None
    fact_version: int | None = Field(default=None, ge=1)
    feedback_scope: FeedbackScope | None = None
    expected_artifact_type: str | None = None
    expected_artifact_id: str | None = None
    expected_project_id: str | None = None


class AttributionResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    attributed: bool
    status: Literal["attributed", "unattributable", "ambiguous", "non_fact_feedback"]
    classification: str
    outcome_ids: list[str] = Field(default_factory=list)


def _outcome_id(idempotency_key: str, target: str) -> str:
    digest = hashlib.sha256(f"{idempotency_key}|{target}".encode("utf-8")).hexdigest()
    return "lo_" + digest[:24]


def attribute_outcome(conn: sqlite3.Connection, request: AttributionRequest) -> AttributionResult:
    revision = conn.execute(
        """SELECT * FROM output_revisions WHERE output_revision_id=? AND trace_id=?
           AND operator_id=? AND surface=?""",
        (request.output_revision_id, request.trace_id, request.operator_id, request.surface),
    ).fetchone()
    if not revision:
        return AttributionResult(
            attributed=False, status="unattributable", classification="unrelated",
        )
    revision_data = dict(revision)
    if request.expected_artifact_type is not None and (
        revision_data["artifact_type"] != request.expected_artifact_type
    ):
        return AttributionResult(
            attributed=False, status="unattributable", classification="artifact_mismatch",
        )
    if request.expected_artifact_id is not None and (
        revision_data["artifact_id"] != request.expected_artifact_id
    ):
        return AttributionResult(
            attributed=False, status="unattributable", classification="artifact_mismatch",
        )
    if request.feedback_scope in ("style", "unrelated"):
        return AttributionResult(
            attributed=False, status="non_fact_feedback",
            classification="style_only" if request.feedback_scope == "style" else "unrelated",
        )
    exposures = list_exposures(
        conn, trace_id=request.trace_id, output_revision_id=request.output_revision_id,
        operator_id=request.operator_id, surface=request.surface,
    )
    if request.expected_project_id is not None and any(
        item.project_id != request.expected_project_id for item in exposures
    ):
        return AttributionResult(
            attributed=False, status="unattributable", classification="project_mismatch",
        )
    if request.fact_id is not None or request.fact_version is not None:
        if request.fact_id is None or request.fact_version is None:
            return AttributionResult(
                attributed=False, status="unattributable", classification="invalid_selector",
            )
        exposures = [item for item in exposures if (
            item.fact_id == request.fact_id and item.fact_version == request.fact_version
        )]
    elif request.feedback_scope != "all_rendered" and len(exposures) > 1:
        return AttributionResult(
            attributed=False, status="ambiguous", classification="ambiguous",
        )
    if not exposures:
        return AttributionResult(
            attributed=False, status="unattributable", classification="unrelated",
        )
    ids: list[str] = []
    for exposure in exposures:
        assert exposure.exposure_id is not None
        outcome_id = _outcome_id(request.idempotency_key, exposure.exposure_id)
        conn.execute(
            """INSERT OR IGNORE INTO learning_outcomes
               (outcome_id,idempotency_key,exposure_id,trace_id,output_revision_id,
                operator_id,surface,fact_id,fact_version,feedback_id,outcome_type,
                outcome_value,reason,actor_id,feedback_scope)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (outcome_id, request.idempotency_key, exposure.exposure_id,
             request.trace_id, request.output_revision_id, request.operator_id,
             request.surface, exposure.fact_id, exposure.fact_version,
             request.feedback_id, request.outcome_type, request.outcome_value,
             request.reason, request.actor_id, request.feedback_scope),
        )
        ids.append(outcome_id)
    return AttributionResult(
        attributed=True, status="attributed", classification=request.outcome_type,
        outcome_ids=ids,
    )
