"""Offline-only canonical source projection for grounding-repair Policy-v7.

Policy-v7 does not filter model prose. It rebuilds a narrow portfolio action-ownership
answer directly from one complete structured ``query_portfolio`` result. The module is
research-only and must never be imported by serving runtime modules.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, fields
from datetime import datetime
from typing import Any, Iterable

from src.models.enums import Milestone, ProjectStatus

from .grounding_repair_certificate_v5_rules import CITATION_RE, INTERNAL_MARKER_RE

POLICY_VERSION_V7 = "workspace-grounding-repair-certificate-v7"
MIN_EMITTED_ROWS_V7 = 3
MAX_EMITTED_ROWS_V7 = 20
_TERMINAL_STATUSES = frozenset(
    item.value.casefold() for item in ProjectStatus.terminal_statuses()
)
_PROJECT_STATUSES = frozenset(item.value.casefold() for item in ProjectStatus)
_MILESTONES = frozenset(item.value for item in Milestone)
_OWNER_ORDER = {"internal": 0, "external": 1, "shared": 2}
_OWNER_LABEL = {"internal": "Internal", "external": "External", "shared": "Shared"}
_SAFE_SOURCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
_SAFE_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")

_ACTION_OWNERSHIP_RE = re.compile(
    r"\b(?:project\s+actions?|recorded\s+actions?|next\s+actions?)\b.*"
    r"\b(?:internal|external|owner|ownership|ball)\b|"
    r"\b(?:internal|external)\b.*\b(?:actions?|dependencies|owner|ownership|ball)\b",
    re.IGNORECASE | re.DOTALL,
)
_PORTFOLIO_SCOPE_RE = re.compile(
    r"\b(?:portfolio|workspace|all\s+projects?|project\s+actions?)\b",
    re.IGNORECASE,
)
_UNSUPPORTED_QUESTION_FEATURES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("unsupported_blocker_lens", re.compile(r"\bemphasize\s+blockers?\b", re.IGNORECASE)),
    (
        "unsupported_unanswered_communication_lens",
        re.compile(
            r"\bemphasize\s+(?:unanswered\s+communications?|recorded\s+communication\s+gaps?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "unsupported_followup_date_lens",
        re.compile(r"\bemphasize\s+recorded\s+follow[- ]?up\s+dates?\b", re.IGNORECASE),
    ),
    (
        "unsupported_contradiction_lens",
        re.compile(
            r"\bemphasize\s+(?:status\s+contradictions?|cross[- ]record\s+consistency)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "unsupported_unresolved_only_lens",
        re.compile(r"\bemphasize\s+unresolved\s+work\s+only\b", re.IGNORECASE),
    ),
    (
        "unsupported_stale_lens",
        re.compile(r"\bemphasize\s+stale\s+activity\s+signals?\b", re.IGNORECASE),
    ),
)


@dataclass(frozen=True, slots=True)
class CanonicalCandidateV7:
    """Plaintext-in-memory canonical answer plus non-sensitive decision metadata."""

    candidate_answer: str
    eligible: bool
    reason: str
    query_scope: str = ""
    source_tool: str = ""
    source_row_count: int = 0
    emitted_row_count: int = 0
    omitted_terminal_row_count: int = 0
    cited_source_count: int = 0
    owner_group_count: int = 0
    as_of_present: bool = False

    def as_metadata(self) -> dict[str, str | int | bool]:
        return {
            item.name: getattr(self, item.name)
            for item in fields(self)
            if item.name != "candidate_answer"
        }


def _reject(reason: str) -> CanonicalCandidateV7:
    return CanonicalCandidateV7(candidate_answer="", eligible=False, reason=reason)


def _safe_text(value: Any, *, limit: int) -> str | None:
    text = str(value or "").strip()
    if not text or len(text) > limit:
        return None
    if (
        "\n" in text
        or "\r" in text
        or CITATION_RE.search(text)
        or INTERNAL_MARKER_RE.search(text)
    ):
        return None
    return text


def _parse_timestamp(value: Any) -> tuple[str, datetime] | None:
    text = _safe_text(value, limit=100)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return text, parsed


def _source_index(
    sources: Iterable[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], bool]:
    index: dict[str, dict[str, Any]] = {}
    duplicate = False
    for source in sources:
        if not isinstance(source, dict):
            continue
        source_id = str(source.get("source_id") or "").strip()
        if not source_id:
            continue
        if source_id in index:
            duplicate = True
            continue
        index[source_id] = source
    return index, duplicate


def _complete_all_project_query(
    item: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    if str(item.get("tool") or "") != "query_portfolio":
        return None
    arguments = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}
    result = item.get("result") if isinstance(item.get("result"), dict) else None
    if result is None or result.get("error"):
        return None
    raw_rows = result.get("results")
    if not isinstance(raw_rows, list) or any(not isinstance(row, dict) for row in raw_rows):
        return None
    rows = list(raw_rows)
    total = result.get("total")
    result_count = result.get("result_count")
    public_filters = (
        "search_text",
        "project_status",
        "current_milestone",
        "ball_with",
        "market",
        "client_name",
    )
    requested_include_terminal = result.get("requested_include_terminal") is True
    trusted_all_projects = result.get("trusted_all_projects_requested") is True
    if (
        result.get("effective_include_terminal") is not True
        or not (requested_include_terminal or trusted_all_projects)
        or result.get("next_cursor") not in (None, "")
        or not isinstance(total, int)
        or isinstance(total, bool)
        or not isinstance(result_count, int)
        or isinstance(result_count, bool)
        or total != result_count
        or result_count != len(rows)
        or total < 1
        or bool(result.get("filters"))
        or any(str(arguments.get(key) or "").strip() for key in public_filters)
        or arguments.get("cursor") not in (None, "")
        or (
            not trusted_all_projects
            and arguments.get("include_terminal") is not True
        )
        or str(arguments.get("_assistant_scope_mode") or "portfolio") != "portfolio"
        or bool(arguments.get("_assistant_scope_project_ids"))
    ):
        return None
    limit = arguments.get("limit")
    if limit is not None and (
        not isinstance(limit, int) or isinstance(limit, bool) or limit < result_count
    ):
        return None
    return result, rows


def _validate_source_identity(
    *,
    row: dict[str, Any],
    source_index: dict[str, dict[str, Any]],
) -> tuple[str, str, str, str] | None:
    source_id = _safe_text(row.get("source_id"), limit=200)
    project_id = _safe_text(row.get("project_id"), limit=100)
    status = _safe_text(row.get("project_status"), limit=100)
    updated = _parse_timestamp(row.get("updated_at"))
    if source_id is None or project_id is None or status is None or updated is None:
        return None
    updated_at, _updated_dt = updated
    if (
        not _SAFE_SOURCE_ID_RE.fullmatch(source_id)
        or not _SAFE_PROJECT_ID_RE.fullmatch(project_id)
        or status.casefold() not in _PROJECT_STATUSES
    ):
        return None
    source = source_index.get(source_id)
    if (
        source is None
        or str(source.get("kind") or "").strip() != "project"
        or str(source.get("project_id") or "").strip() != project_id
        or str(source.get("occurred_at") or "").strip() != updated_at
        or str(source.get("locator") or "").strip()
        != f"project:{project_id}:{updated_at}"
    ):
        return None
    return source_id, project_id, status, updated_at


def _canonical_row(
    row: dict[str, Any],
    source_index: dict[str, dict[str, Any]],
    *,
    as_of_dt: datetime,
) -> dict[str, str] | None:
    identity = _validate_source_identity(row=row, source_index=source_index)
    milestone = _safe_text(row.get("current_milestone"), limit=160)
    owner = _safe_text(row.get("ball_with"), limit=100)
    action = _safe_text(row.get("next_action"), limit=500)
    last_activity = _parse_timestamp(row.get("last_activity_at"))
    updated = _parse_timestamp(row.get("updated_at"))
    if (
        identity is None
        or milestone is None
        or owner is None
        or action is None
        or last_activity is None
        or updated is None
    ):
        return None
    source_id, project_id, status, updated_at = identity
    last_activity_at, last_activity_dt = last_activity
    _updated_text, updated_dt = updated
    owner_key = owner.casefold()
    if (
        owner_key not in _OWNER_ORDER
        or status.casefold() in _TERMINAL_STATUSES
        or milestone not in _MILESTONES
        or updated_dt > as_of_dt
        or last_activity_dt > as_of_dt
    ):
        return None
    return {
        "source_id": source_id,
        "project_id": project_id,
        "status": status,
        "milestone": milestone,
        "owner": _OWNER_LABEL[owner_key],
        "owner_key": owner_key,
        "action": action,
        "last_activity": last_activity_at,
        "updated_at": updated_at,
    }


def build_grounding_repair_candidate_v7(
    *,
    question: str,
    sources: Iterable[dict[str, Any]],
    tool_results: Iterable[dict[str, Any]] = (),
) -> CanonicalCandidateV7:
    """Build a canonical action-ownership answer from a complete all-project query."""

    question_text = str(question or "")
    if (
        not _ACTION_OWNERSHIP_RE.search(question_text)
        or not _PORTFOLIO_SCOPE_RE.search(question_text)
    ):
        return _reject("question_contract_not_supported")
    for reason, pattern in _UNSUPPORTED_QUESTION_FEATURES:
        if pattern.search(question_text):
            return _reject(reason)

    source_index, duplicate_source = _source_index(sources)
    if duplicate_source:
        return _reject("duplicate_source_id")

    complete_queries = [
        selected
        for item in tool_results
        if isinstance(item, dict)
        and (selected := _complete_all_project_query(item)) is not None
    ]
    if len(complete_queries) != 1:
        return _reject(
            "complete_all_project_query_missing"
            if not complete_queries
            else "multiple_complete_all_project_queries"
        )
    result, rows = complete_queries[0]
    summary_source_id = _safe_text(result.get("summary_source_id"), limit=200)
    parsed_as_of = _parse_timestamp(result.get("as_of"))
    if (
        summary_source_id is None
        or parsed_as_of is None
        or not _SAFE_SOURCE_ID_RE.fullmatch(summary_source_id)
    ):
        return _reject("query_summary_contract_failed")
    as_of, as_of_dt = parsed_as_of
    summary_source = source_index.get(summary_source_id)
    summary_metadata = (
        summary_source.get("metadata")
        if isinstance(summary_source, dict)
        and isinstance(summary_source.get("metadata"), dict)
        else {}
    )
    if (
        summary_source is None
        or str(summary_source.get("kind") or "").strip() != "portfolio_query"
        or str(summary_source.get("occurred_at") or "").strip() != as_of
        or summary_metadata.get("filters") not in ({}, None)
        or summary_metadata.get("effective_include_terminal") is not True
        or summary_metadata.get("total") != result.get("total")
        or summary_metadata.get("result_count") != result.get("result_count")
    ):
        return _reject("query_summary_source_invalid")

    emitted: list[dict[str, str]] = []
    omitted_terminal = 0
    seen_projects: set[str] = set()
    seen_sources: set[str] = {summary_source_id}
    for row in rows:
        identity = _validate_source_identity(row=row, source_index=source_index)
        if identity is None:
            return _reject("row_identity_contract_failed")
        source_id, project_id, status, _updated_at = identity
        if project_id in seen_projects or source_id in seen_sources:
            return _reject("duplicate_query_row_identity")
        seen_projects.add(project_id)
        seen_sources.add(source_id)
        if status.casefold() in _TERMINAL_STATUSES:
            omitted_terminal += 1
            continue
        canonical = _canonical_row(row, source_index, as_of_dt=as_of_dt)
        if canonical is None:
            return _reject("nonterminal_row_contract_failed")
        emitted.append(canonical)

    if not (MIN_EMITTED_ROWS_V7 <= len(emitted) <= MAX_EMITTED_ROWS_V7):
        return _reject("emitted_row_budget_not_met")
    if len(emitted) + omitted_terminal != len(rows):
        return _reject("row_partition_incomplete")

    lines = [
        "# Recorded portfolio action ownership",
        "The recorded actions are grouped by the Workspace ball-ownership field; no inferred ownership is added.",
        "",
        "## Scope",
        (
            f"Query scope: the complete unfiltered Workspace portfolio query returned "
            f"{len(rows)} project records as of {as_of}. Terminal records are excluded "
            f"from the non-terminal action list. [[source:{summary_source_id}]]"
        ),
        "",
    ]
    group_count = 0
    for owner_key in ("internal", "external", "shared"):
        group = sorted(
            (row for row in emitted if row["owner_key"] == owner_key),
            key=lambda row: row["project_id"].casefold(),
        )
        if not group:
            continue
        group_count += 1
        lines.append(f"## {_OWNER_LABEL[owner_key]}")
        for row in group:
            lines.append(
                f"- **{row['project_id']}** — recorded next action: {row['action']}; "
                f"ball with: {row['owner']}; status: {row['status']}; milestone: "
                f"{row['milestone']}; last activity: {row['last_activity']}; record updated: "
                f"{row['updated_at']}. [[source:{row['source_id']}]]"
            )
        lines.append("")
    lines.extend((
        "## Evidence boundary",
        (
            "This deterministic view uses only recorded project status, milestone, ball ownership, "
            "next action, and record timestamps; it does not infer completion, blockers, or "
            "unrecorded dependencies."
        ),
        "",
        "## Operator review",
        "- Review the recorded next actions above; no additional recommendation is inferred.",
    ))
    candidate = "\n".join(lines).strip()
    return CanonicalCandidateV7(
        candidate_answer=candidate,
        eligible=True,
        reason="canonical_source_projection_built",
        query_scope="complete_unfiltered_portfolio_nonterminal_actions",
        source_tool="query_portfolio",
        source_row_count=len(rows),
        emitted_row_count=len(emitted),
        omitted_terminal_row_count=omitted_terminal,
        cited_source_count=len(emitted) + 1,
        owner_group_count=group_count,
        as_of_present=True,
    )


__all__ = [
    "CanonicalCandidateV7",
    "MAX_EMITTED_ROWS_V7",
    "MIN_EMITTED_ROWS_V7",
    "POLICY_VERSION_V7",
    "build_grounding_repair_candidate_v7",
]
