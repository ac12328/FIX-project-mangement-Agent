"""Validation and low-risk rebase logic for proposed-update reviews."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from pydantic import ValidationError

from src.db.savepoint import savepoint
from src.models.progress_assessment import AssessmentDecision, ProjectStateAssessment
from src.repositories.project_repo import get_project_field

_PROTECTED_FIELDS = frozenset({"current_milestone"})
_ASSESSMENT_COLUMNS = (
    "assessment_json",
    "policy_version",
    "source_type",
    "source_ref",
)


class _RefreshClaimLost(Exception):
    """A low-risk review ceased to be pending before its rebase."""


def _parse_changes(raw: Any) -> tuple[list[dict[str, Any]], str | None]:
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, json.JSONDecodeError):
        return [], "Review contains malformed proposed changes"
    if not isinstance(parsed, list) or not parsed:
        return [], "Review contains no proposed changes"

    changes: list[dict[str, Any]] = []
    for item in parsed:
        if not isinstance(item, dict) or not isinstance(item.get("field"), str):
            return [], "Review contains malformed proposed changes"
        changes.append(item)
    return changes, None


def _stale_fields(
    conn: sqlite3.Connection,
    project_id: str,
    changes: list[dict[str, Any]],
) -> list[str]:
    stale: list[str] = []
    for change in changes:
        if "old_value" not in change:
            continue
        current = get_project_field(conn, project_id, change["field"])
        expected = change.get("old_value")
        current_text = str(current) if current is not None else None
        expected_text = str(expected) if expected is not None else None
        if current_text != expected_text:
            stale.append(change["field"])
    return stale


def _refresh_stale_baseline(
    conn: sqlite3.Connection,
    update_id: str,
    project_id: str,
    changes: list[dict[str, Any]],
    stale_fields: list[str],
    *,
    commit: bool = True,
) -> bool:
    """Rebase low-risk fields only if the same review remains pending."""
    try:
        with savepoint(conn, ensure_transaction=True):
            stale_set = set(stale_fields)
            refreshed_changes = []
            for change in changes:
                refreshed = dict(change)
                if change["field"] in stale_set:
                    refreshed["old_value"] = get_project_field(
                        conn, project_id, change["field"]
                    )
                refreshed_changes.append(refreshed)
            claimed = conn.execute(
                "UPDATE proposed_updates SET proposed_changes=? WHERE update_id=? "
                "AND decision_outcome='needs_review'",
                (json.dumps(refreshed_changes, ensure_ascii=False), update_id),
            )
            if claimed.rowcount != 1:
                raise _RefreshClaimLost()
    except _RefreshClaimLost:
        return False
    if commit:
        conn.commit()
    return True


def _validate_protected_assessment(
    data: dict[str, Any], changes: list[dict[str, Any]]
) -> str | None:
    if not any(change["field"] in _PROTECTED_FIELDS for change in changes):
        return None

    metadata_present = any(data.get(column) not in (None, "") for column in _ASSESSMENT_COLUMNS)
    if not metadata_present:
        return None
    if any(data.get(column) in (None, "") for column in _ASSESSMENT_COLUMNS):
        return "Evidence-grounded milestone review is missing required assessment metadata"

    try:
        raw_assessment = data["assessment_json"]
        payload = json.loads(raw_assessment) if isinstance(raw_assessment, str) else raw_assessment
        assessment = ProjectStateAssessment.model_validate(payload)
    except (TypeError, json.JSONDecodeError, ValidationError, ValueError):
        return "Evidence-grounded milestone review has invalid assessment metadata"

    if assessment.project_id != data["project_id"]:
        return "Assessment project identity does not match the review"
    if assessment.source_ref != data["source_ref"]:
        return "Assessment source reference does not match the review"
    if assessment.source_type.value != data["source_type"]:
        return "Assessment source type does not match the review"
    if assessment.policy_version != data["policy_version"]:
        return "Assessment policy version does not match the review"
    milestone = assessment.milestone_assessment
    if milestone is None:
        return "Evidence-grounded milestone review lacks a milestone assessment"
    milestone_changes = [
        change for change in changes if change["field"] == "current_milestone"
    ]
    if len(milestone_changes) != 1:
        return "Evidence-grounded milestone review must contain exactly one milestone change"
    change = milestone_changes[0]
    if "old_value" not in change:
        return "Evidence-grounded milestone review requires the proposed old value"
    if milestone.candidate_value != change.get("new_value"):
        return "Assessment milestone candidate does not match the proposed value"
    if milestone.current_value != change.get("old_value"):
        return "Assessment milestone current value does not match the proposed old value"
    if milestone.decision != AssessmentDecision.NEEDS_REVIEW:
        return "Assessment milestone decision is not eligible for protected review approval"
    return None
