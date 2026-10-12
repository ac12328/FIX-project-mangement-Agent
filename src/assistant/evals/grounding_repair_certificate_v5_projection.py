"""In-memory typed source projections for offline Policy-v5 research.

The serving runtime must not import this module. Decrypted material remains in memory;
only aggregate certificate metadata may be persisted by callers.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Iterator, Literal

PROJECT_ID_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:P(?:ROJ(?:ECT)?)?[-_]?\d{2,}|[A-Z]{2,}[A-Z0-9-]*20\d{2})(?![A-Za-z0-9])",
    re.IGNORECASE,
)
MILESTONE_RE = re.compile(r"\bM[1-8](?:_[A-Za-z0-9]+)?\b", re.IGNORECASE)
_DATETIME_RE = re.compile(
    r"(?<!\d)(?P<iso>20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}"
    r"(?:[T\s]\d{1,2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?"
    r"(?:\s*(?:Z|UTC|HKT|[+-]\d{2}:?\d{2}))?)?)(?![+\-\d])|"
    r"(?<!\d)(?P<dmy>\d{1,2}\s+(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|"
    r"May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|"
    r"Nov(?:ember)?|Dec(?:ember)?)\s+20\d{2})(?!\d)|"
    r"(?<!\d)(?P<mdy>(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
    r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|"
    r"Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2},?\s+20\d{2})(?!\d)",
    re.IGNORECASE,
)
DATE_RE = _DATETIME_RE
NUMBER_RE = re.compile(
    r"(?<![\w.])[-+]?\d{1,3}(?:,\d{3})+(?:\.\d+)?%?(?!\w)|"
    r"(?<![\w.])[-+]?\d+(?:\.\d+)?%?(?!\w)"
)
_CLOCK_RE = re.compile(
    r"(?<![\d:])\d{1,2}:\d{2}(?::\d{2})?\s*(?:Z|UTC|HKT)(?![A-Za-z])",
    re.IGNORECASE,
)
_LIST_RANGE_RE = re.compile(
    r"\b(?:items?|cases?|points?|questions?)\s+(\d+)\s*"
    r"(?:&|and|to|[-\u2013\u2014])\s*(\d+)(?!\d)",
    re.IGNORECASE,
)
_DURATION_RE = re.compile(
    r"(?<![\w.])([-+]?\d+(?:\.\d+)?)\s*(?:d|days?|h|hrs?|hours?)(?![A-Za-z])",
    re.IGNORECASE,
)
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_/-]*")
_STOPWORDS = frozenset(
    {
        "the", "and", "for", "from", "with", "that", "this", "these", "those", "into",
        "project", "projects", "record", "records", "workspace", "source", "sources",
        "status", "current", "currently", "latest", "overall", "based", "as", "of", "at",
        "is", "are", "was", "were", "be", "been", "has", "have", "had", "to", "on", "in",
        "a", "an", "or", "by", "it", "its", "their", "there", "than", "then",
    }
)

QUERY_SOURCE_KINDS = frozenset(
    {"portfolio", "portfolio_query", "project_work_query", "timeline_query", "unanswered_thread_query"}
)
CURRENT_SOURCE_KINDS = frozenset(
    {
        "project", "project_snapshot", "portfolio", "portfolio_query", "project_work",
        "project_work_query", "timeline_query", "unanswered_thread_query", "milestone_policy",
        "proposed_update", "email", "activity", "milestone", "evidence", "party",
    }
)
AGGREGATE_SOURCE_KINDS = QUERY_SOURCE_KINDS
RANKING_SOURCE_KINDS = frozenset({"portfolio", "portfolio_query"})
READINESS_SOURCE_KINDS = frozenset({"milestone_policy", "proposed_update"})
QUERY_TOOL_NAMES = frozenset(
    {"query_portfolio", "portfolio_attention", "list_project_work", "list_unanswered_threads", "search_project_timeline"}
)

_SAFE_VALUE_FIELDS = frozenset(
    {
        "id", "project_id", "client_name", "project_status", "current_milestone", "ball_with",
        "market", "next_action", "target_go_live", "updated_at", "last_activity_at",
        "priority_score", "days_inactive", "attention_reasons", "flags", "status",
        "source_status", "title", "description", "summary", "snippet", "body_snippet", "body", "subject",
        "direction", "occurred_at", "created_at", "changed_at", "completed_at", "resolved_at",
        "due_at", "as_of", "notes", "evidence_at",
        "work_type", "work_id", "owner", "is_overdue", "candidate_definition",
        "latest_email_id", "actionability", "action_required", "suggested_action", "confidence", "category",
        "suppressed", "suppression_reason", "broker_code", "connection_type", "project_tier",
        "content_summary", "content_text", "proposed_changes", "field", "old_value", "new_value",
        "risk_level", "decision_counts", "reason", "priority_gap", "uncertainties",
        "client_email", "sla_limit", "logbook", "operator_id", "work_item_id",
        "sender_email", "received_at", "processed_at", "record_type",
        "actor", "surface", "action_type", "object_ref", "record_id",
        "org_id", "org_name", "org_type", "is_primary",
        "evidence_id", "evidence_type", "changed_by", "proposed_update_id",
        "update_id", "source_type", "requires_operator_review", "is_superseded",
        "decision_outcome", "decided_at", "superseded_count",
        "created_by", "escalation_level", "resolution_note",
        "criterion_id", "predicate", "minimum_evidence_level",
        "accepted_evidence_predicates", "signoff_authorities",
        "named_authority_required_for_signoff", "guidance", "provenance_guidance",
        "total", "result_count", "projects_scanned", "project_ids_scanned", "attention_count",
        "scope_mode", "requested_include_terminal", "effective_include_terminal",
        "trusted_all_projects_requested", "min_age_hours", "age_hours", "message_count", "kinds", "assessment_status",
        "target_milestone", "decision", "decision_reasons", "criteria_counts", "criterion_id",
        "result", "explanation", "codes", "assessment_codes", "policy_version",
        "unresolved_source_refs", "required_exit_criteria", "provenance_quality", "source_ref",
        "from_milestone", "to_milestone", "evidence_ref", "party_role", "candidate_state",
        "thread_key", "filters", "search_text", "include_terminal", "include_on_track", "limit",
    }
)
_METADATA_FIELDS = frozenset(
    {
        "as_of", "total", "result_count", "projects_scanned", "attention_count", "scope_mode",
        "requested_include_terminal", "effective_include_terminal", "trusted_all_projects_requested",
        "min_age_hours", "status", "due_at", "provenance_quality", "policy_version",
        "decision_outcome", "flags", "snapshot_reconstructed", "snapshot_rewound_fields", "kinds",
        "project_ids", "assessment_status", "target_go_live",
        "query_tool", "query_rank", "query_row_count", "query_distinct_project_count",
        "query_summary_source_id", "collection_latest",
    }
)
_TEMPORAL_FIELDS = frozenset(
    {"occurred_at", "as_of", "updated_at", "created_at", "changed_at", "completed_at",
     "resolved_at", "due_at", "target_go_live", "last_activity_at", "evidence_at",
     "received_at", "processed_at", "decided_at"}
)
_PROJECT_STATUS_FIELDS = frozenset({"project_status"})
_OWNERSHIP_FIELDS = frozenset({"ball_with"})
_MILESTONE_FIELDS = frozenset({"current_milestone", "from_milestone", "to_milestone", "target_milestone"})


@dataclass(frozen=True, slots=True)
class SourceProjectionV5:
    source_id: str
    kind: str
    text: str
    tokens: frozenset[str]
    scalars: frozenset[str]
    dates: frozenset[str]
    project_ids: frozenset[str]
    milestones: frozenset[str]
    project_statuses: frozenset[str]
    ownership_values: frozenset[str]
    temporal_values: frozenset[str]
    fields: frozenset[str]
    tools: frozenset[str]
    metadata: dict[str, str | int | float | bool] = field(default_factory=dict)
    direct_scalars: frozenset[str] = frozenset()
    query_summary_scalars: frozenset[str] = frozenset()
    direct_tokens: frozenset[str] = frozenset()
    query_summary_tokens: frozenset[str] = frozenset()
    direct_fields: frozenset[str] = frozenset()
    query_summary_fields: frozenset[str] = frozenset()
    direct_temporal_values: frozenset[str] = frozenset()
    query_summary_temporal_values: frozenset[str] = frozenset()
    direct_text: str = ""
    query_summary_text: str = ""

    @property
    def has_temporal_support(self) -> bool:
        return bool(self.temporal_values)

    @property
    def is_query(self) -> bool:
        return self.kind in QUERY_SOURCE_KINDS or bool(self.tools & QUERY_TOOL_NAMES)

    @property
    def is_query_summary(self) -> bool:
        return bool(self.metadata.get("is_query_summary"))

    @property
    def query_tool(self) -> str:
        return str(self.metadata.get("query_tool") or "")

    @property
    def query_rank(self) -> int | None:
        raw = self.metadata.get("query_rank")
        return int(raw) if isinstance(raw, (int, float)) and int(raw) > 0 else None

    @property
    def query_summary_source_id(self) -> str:
        return str(self.metadata.get("query_summary_source_id") or "")

    @property
    def query_ordering(self) -> str:
        return str(self.metadata.get("query_ordering") or "")

    @property
    def collection_latest(self) -> bool:
        return bool(self.metadata.get("collection_latest"))

    @property
    def current_snapshot(self) -> bool:
        return bool(self.metadata.get("current_snapshot"))

    @property
    def scope_all_projects(self) -> bool:
        return bool(self.metadata.get("scope_all_projects"))

    @property
    def query_scope(self) -> str:
        return str(self.metadata.get("query_scope") or "")


MaterialRoleV5 = Literal["direct", "query_summary", "metadata"]


@dataclass(frozen=True, slots=True)
class ProjectionMaterialV5:
    role: MaterialRoleV5
    value: dict[str, Any]


@dataclass(slots=True)
class ProjectionCacheV5:
    """Reusable, plaintext-in-memory projection cache for one replay payload."""

    records: dict[str, dict[str, Any]]
    duplicate_source_id_count: int
    material_index: dict[str, tuple[ProjectionMaterialV5, ...]]
    material_tools: dict[str, frozenset[str]]
    known_project_ids: frozenset[str]
    all_projects_requested: bool
    projections: dict[str, SourceProjectionV5] = field(default_factory=dict)

    @classmethod
    def build(
        cls,
        sources: Iterable[dict[str, Any]],
        tool_results: Iterable[dict[str, Any]] = (),
    ) -> "ProjectionCacheV5":
        records: dict[str, dict[str, Any]] = {}
        duplicate_count = 0
        direct_project_ids: set[str] = set()
        for source in sources:
            if not isinstance(source, dict):
                continue
            source_id = str(source.get("source_id") or "").strip()
            if not source_id:
                continue
            if source_id in records:
                duplicate_count += 1
                continue
            records[source_id] = source
            project_id = _canonical_project_id(str(source.get("project_id") or ""))
            if project_id:
                direct_project_ids.add(project_id)
        material_index, material_tools, all_projects_requested = _direct_material_index(tool_results)
        return cls(
            records=records,
            duplicate_source_id_count=duplicate_count,
            material_index={key: tuple(value) for key, value in material_index.items()},
            material_tools={key: frozenset(value) for key, value in material_tools.items()},
            known_project_ids=frozenset(direct_project_ids),
            all_projects_requested=all_projects_requested,
        )

    def materialize(self, source_ids: Iterable[str]) -> dict[str, SourceProjectionV5]:
        selected: dict[str, SourceProjectionV5] = {}
        for source_id in dict.fromkeys(str(item) for item in source_ids):
            projection = self.projections.get(source_id)
            if projection is None:
                source = self.records.get(source_id)
                if source is None:
                    continue
                projection = _build_projection(
                    source_id=source_id,
                    source=source,
                    materials=self.material_index.get(source_id, ()),
                    tools=self.material_tools.get(source_id, frozenset()),
                )
                self.projections[source_id] = projection
            selected[source_id] = projection
        return selected


def _metadata_dict(source: dict[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for key in ("metadata", "metadata_json"):
        raw = source.get(key)
        if isinstance(raw, dict):
            values.update(raw)
        elif isinstance(raw, str) and raw.lstrip().startswith("{"):
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                parsed = None
            if isinstance(parsed, dict):
                values.update(parsed)
    return values


def _source_ids_from_node(node: dict[str, Any]) -> tuple[str, ...]:
    values = [str(node.get("source_id") or "").strip()]
    raw_values = node.get("source_ids")
    if isinstance(raw_values, list):
        values.extend(str(value).strip() for value in raw_values)
    return tuple(dict.fromkeys(value for value in values if value))


def _query_ordering(tool: str) -> str:
    return {
        "portfolio_attention": "priority_desc",
        "search_project_timeline": "occurred_desc",
        "list_project_work": "created_desc",
        "list_unanswered_threads": "occurred_asc",
    }.get(tool, "")


def _query_scope(
    tool: str,
    result: dict[str, Any],
    arguments: dict[str, Any],
) -> tuple[str, bool]:
    if tool == "query_portfolio":
        all_projects = bool(
            result.get("trusted_all_projects_requested")
            or result.get("effective_include_terminal")
            or arguments.get("include_terminal")
        )
        return ("all_projects" if all_projects else "active_projects"), all_projects
    if tool == "portfolio_attention":
        return ("selected_projects" if arguments.get("project_ids") else "active_projects"), False
    if tool in {"list_project_work", "search_project_timeline"}:
        return "single_project", False
    if tool == "list_unanswered_threads":
        all_projects = bool(arguments.get("include_terminal"))
        return ("all_projects" if all_projects else "active_projects"), all_projects
    return "", False


def _material_nodes(
    tool: str,
    result: dict[str, Any],
) -> list[tuple[dict[str, Any], dict[str, Any] | None]]:
    """Return documented result rows, never a recursively shared result root."""
    output: list[tuple[dict[str, Any], dict[str, Any] | None]] = []

    def add_list(key: str, *, latest_first: bool = False, query_rows: bool = False) -> None:
        raw = result.get(key)
        if not isinstance(raw, list):
            return
        rows = [item for item in raw if isinstance(item, dict)]
        distinct_projects = {
            _canonical_project_id(str(item.get("project_id") or "")) for item in rows
            if str(item.get("project_id") or "").strip()
        }
        summary_source_id = str(result.get("summary_source_id") or "")
        ordering = _query_ordering(tool)
        for index, node in enumerate(rows):
            derived: dict[str, Any] = {}
            if latest_first and index == 0:
                derived["collection_latest"] = True
            if query_rows:
                derived.update({
                    "query_tool": tool,
                    "query_rank": index + 1,
                    "query_row_count": len(rows),
                    "query_distinct_project_count": len(distinct_projects),
                    "query_summary_source_id": summary_source_id,
                    "query_ordering": ordering,
                    "current_snapshot": tool in {"query_portfolio", "portfolio_attention"},
                    "collection_latest": bool(
                        index == 0 and ordering in {"occurred_desc", "created_desc"}
                    ),
                })
            output.append((node, derived or None))

    if tool in QUERY_TOOL_NAMES:
        add_list("results", query_rows=True)
    elif tool == "get_project_brief":
        project = result.get("project")
        if isinstance(project, dict):
            output.append((project, {"collection_latest": True, "current_snapshot": True}))
        add_list("parties")
        add_list("milestone_history", latest_first=True)
        add_list("recent_activity", latest_first=True)
        add_list("recent_emails", latest_first=True)
    elif tool == "assess_milestone_readiness":
        add_list("required_exit_criteria")
        add_list("criteria")
    elif tool == "get_project_evidence_audit":
        current_truth = result.get("current_truth")
        if isinstance(current_truth, dict):
            output.append((current_truth, {"collection_latest": True, "current_snapshot": True}))
        add_list("evidence", latest_first=True)
        add_list("proposed_updates", latest_first=True)
        add_list("milestone_history", latest_first=True)
        conflicts = result.get("conflicts")
        if isinstance(conflicts, list):
            add_list("conflicts")
        elif isinstance(conflicts, dict):
            output.append((conflicts, None))
    return output


def _shared_result_context(result: dict[str, Any]) -> dict[str, Any]:
    """Return only query-level fields for the dedicated summary source."""
    shared: dict[str, Any] = {}
    for key, raw in result.items():
        if key in {
            "results", "project", "parties", "milestone_history", "recent_activity",
            "recent_emails", "evidence", "proposed_updates", "current_truth", "criteria",
            "required_exit_criteria", "citation_instruction", "conflicts",
        }:
            continue
        if key in _SAFE_VALUE_FIELDS and isinstance(raw, (str, int, float, bool)) and raw not in (None, ""):
            shared[key] = raw
        elif key in {"filters", "criteria_counts"} and isinstance(raw, dict):
            shared[key] = raw
    return shared


def _direct_material_index(
    tool_results: Iterable[dict[str, Any]],
) -> tuple[dict[str, list[ProjectionMaterialV5]], dict[str, set[str]], bool]:
    index: dict[str, list[ProjectionMaterialV5]] = {}
    tools: dict[str, set[str]] = {}
    all_projects_requested = False
    for item in tool_results:
        if not isinstance(item, dict):
            continue
        tool = str(item.get("tool") or "")
        arguments = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}
        result = item.get("result") if isinstance(item.get("result"), dict) else item
        if not isinstance(result, dict):
            continue
        scope_mode, scope_all_projects = _query_scope(tool, result, arguments)
        all_projects_requested = all_projects_requested or scope_all_projects
        summary_source_id = str(result.get("summary_source_id") or "").strip()
        if summary_source_id:
            summary = _shared_result_context(result)
            summary.update({
                "is_query_summary": tool in QUERY_TOOL_NAMES,
                "query_tool": tool if tool in QUERY_TOOL_NAMES else "",
                "query_summary_source_id": summary_source_id,
                "query_row_count": len([row for row in result.get("results", []) if isinstance(row, dict)]),
                "query_distinct_project_count": len({
                    _canonical_project_id(str(row.get("project_id") or ""))
                    for row in result.get("results", []) if isinstance(row, dict) and row.get("project_id")
                }),
                "query_ordering": _query_ordering(tool),
                "query_scope": scope_mode,
                "scope_all_projects": scope_all_projects,
                "collection_latest": True,
                "current_snapshot": tool in {"query_portfolio", "portfolio_attention"},
            })
            index.setdefault(summary_source_id, []).append(ProjectionMaterialV5("query_summary", summary))
            tools.setdefault(summary_source_id, set()).add(tool)
        for node, derived in _material_nodes(tool, result):
            source_ids = _source_ids_from_node(node)
            if not source_ids:
                continue
            for source_id in source_ids:
                index.setdefault(source_id, []).append(ProjectionMaterialV5("direct", node))
                if derived:
                    metadata = dict(derived)
                    metadata.setdefault("query_scope", scope_mode)
                    metadata.setdefault("scope_all_projects", scope_all_projects)
                    index[source_id].append(ProjectionMaterialV5("metadata", metadata))
                tools.setdefault(source_id, set()).add(tool)
    return index, tools, all_projects_requested


def _iter_safe_fields(value: Any, *, max_depth: int = 5) -> Iterator[tuple[str, Any]]:
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        if depth > max_depth:
            continue
        if isinstance(current, dict):
            for key, raw in current.items():
                if key in {"url", "locator", "citation_instruction"}:
                    continue
                if key in _SAFE_VALUE_FIELDS:
                    if isinstance(raw, (str, int, float, bool)) and raw not in (None, ""):
                        yield key, raw
                    elif isinstance(raw, list) and len(raw) <= 100:
                        for child in raw:
                            if isinstance(child, (str, int, float, bool)) and child not in (None, ""):
                                yield key, child
                if isinstance(raw, (dict, list)):
                    stack.append((raw, depth + 1))
        elif isinstance(current, list):
            for item in current[:100]:
                if isinstance(item, (dict, list)):
                    stack.append((item, depth + 1))


def _canonical_project_id(value: str) -> str:
    return str(value).strip().upper().replace("_", "-")


def _canonical_project_ids(value: str) -> set[str]:
    return {_canonical_project_id(match.group(0)) for match in PROJECT_ID_RE.finditer(value)}


def _canonical_milestone(value: str) -> str:
    return str(value).strip().upper()


def canonical_milestones(value: str) -> set[str]:
    milestones = {_canonical_milestone(match.group(0)) for match in MILESTONE_RE.finditer(value)}
    milestones.update(item.split("_", 1)[0] for item in tuple(milestones))
    return milestones


def normalize_temporal(value: str) -> set[str]:
    candidate = unicodedata.normalize("NFKC", str(value)).strip()
    candidate = re.sub(r"\s+", " ", candidate)
    output: set[str] = set()
    zone_suffix = None
    zone_match = re.search(r"\s*(HKT|UTC)$", candidate, re.IGNORECASE)
    if zone_match:
        zone_suffix = zone_match.group(1).upper()
        candidate = candidate[: zone_match.start()].strip()
    iso_candidate = candidate.replace("/", "-")
    if re.fullmatch(r"20\d{2}\.\d{1,2}\.\d{1,2}", iso_candidate):
        iso_candidate = iso_candidate.replace(".", "-")
    if iso_candidate.endswith("Z"):
        iso_candidate = iso_candidate[:-1] + "+00:00"
    elif re.search(r"[+-]\d{4}$", iso_candidate):
        iso_candidate = iso_candidate[:-5] + iso_candidate[-5:-2] + ":" + iso_candidate[-2:]
    try:
        parsed = datetime.fromisoformat(iso_candidate)
    except ValueError:
        parsed = None
    if parsed is not None:
        output.add(f"date:{parsed.date().isoformat()}")
        if parsed.hour or parsed.minute or parsed.second or "T" in candidate or re.search(r"\s\d{1,2}:\d{2}", candidate):
            if parsed.tzinfo is None and zone_suffix:
                parsed = parsed.replace(tzinfo=timezone(timedelta(hours=8) if zone_suffix == "HKT" else timedelta(0)))
            if parsed.tzinfo is not None:
                utc_minute = parsed.astimezone(timezone.utc).replace(second=0, microsecond=0)
                output.add("instant:" + utc_minute.isoformat(timespec="minutes").replace("+00:00", "Z"))
        return output
    normalized_words = candidate.replace("/", "-").replace(".", "-")
    for fmt in (
        "%Y-%m-%d", "%d %B %Y", "%d %b %Y", "%B %d, %Y", "%B %d %Y",
        "%b %d, %Y", "%b %d %Y",
    ):
        try:
            output.add(f"date:{datetime.strptime(normalized_words, fmt).date().isoformat()}")
            break
        except ValueError:
            continue
    return output


def normalize_date(value: str) -> str | None:
    for item in normalize_temporal(value):
        if item.startswith("date:"):
            return item.removeprefix("date:")
    return None


def normalized_dates(value: str) -> set[str]:
    output: set[str] = set()
    text = unicodedata.normalize("NFKC", str(value))
    for match in _DATETIME_RE.finditer(text):
        output.update(normalize_temporal(match.group(0)))
    return output


def _normalize_decimal(value: str) -> str | None:
    raw = value.replace(",", "").strip()
    percent = raw.endswith("%")
    if percent:
        raw = raw[:-1]
    try:
        number = Decimal(raw)
    except InvalidOperation:
        return None
    normalized = format(number.normalize(), "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    if normalized == "-0":
        normalized = "0"
    return ("percent:" if percent else "number:") + normalized


def normalized_scalars(value: str) -> set[str]:
    text = unicodedata.normalize("NFKC", str(value))
    date_matches = tuple(_DATETIME_RE.finditer(text))
    clock_matches = tuple(_CLOCK_RE.finditer(text))
    scalars: set[str] = set()
    for match in date_matches:
        scalars.update(normalize_temporal(match.group(0)))
    for match in _LIST_RANGE_RE.finditer(text):
        for raw in match.groups():
            normalized = _normalize_decimal(raw)
            if normalized:
                scalars.add(normalized)
    for match in _DURATION_RE.finditer(text):
        normalized = _normalize_decimal(match.group(1))
        if normalized:
            scalars.add(normalized)
    identity_matches = tuple(PROJECT_ID_RE.finditer(text))
    milestone_matches = tuple(MILESTONE_RE.finditer(text))
    excluded_spans = tuple(
        match.span()
        for match in (*date_matches, *clock_matches, *identity_matches, *milestone_matches)
    )
    for match in NUMBER_RE.finditer(text):
        start, end = match.span()
        if any(start >= left and end <= right for left, right in excluded_spans):
            continue
        normalized = _normalize_decimal(match.group(0))
        if normalized:
            scalars.add(normalized)
    return scalars


def claim_tokens(value: str) -> set[str]:
    text = unicodedata.normalize("NFKC", str(value)).replace("_", " ").replace("-", " ")
    output: set[str] = set()
    for token in _TOKEN_RE.findall(text.casefold()):
        token = token.strip("_/-")
        if len(token) < 2 or token in _STOPWORDS or token.isdecimal():
            continue
        if token.isascii() and len(token) > 4 and token.endswith("s"):
            token = token[:-1]
        output.add(token)
    return output


def _build_projection(
    *,
    source_id: str,
    source: dict[str, Any],
    materials: Iterable[ProjectionMaterialV5],
    tools: frozenset[str],
) -> SourceProjectionV5:
    direct_values: list[str] = []
    summary_values: list[str] = []
    fields: set[str] = set()
    direct_fields: set[str] = set()
    summary_fields: set[str] = set()
    project_ids: set[str] = set()
    milestones: set[str] = set()
    project_statuses: set[str] = set()
    ownership_values: set[str] = set()
    temporal_values: set[str] = set()
    direct_temporal_values: set[str] = set()
    summary_temporal_values: set[str] = set()
    metadata_raw = _metadata_dict(source)
    metadata: dict[str, str | int | float | bool] = {}
    material_list = tuple(materials)
    is_query_summary = any(
        item.role == "query_summary" and bool(item.value.get("is_query_summary"))
        for item in material_list
    )

    def accept(key: str, raw: Any, *, role: MaterialRoleV5) -> None:
        fields.add(key)
        if role == "direct":
            direct_fields.add(key)
        elif role == "query_summary":
            summary_fields.add(key)
        if key in _METADATA_FIELDS or key in {
            "is_query_summary", "query_ordering", "query_scope", "scope_all_projects",
            "current_snapshot",
        }:
            if isinstance(raw, (str, int, float, bool)):
                metadata.setdefault(key, raw)
        if role == "metadata":
            if key in _TEMPORAL_FIELDS:
                temporal_values.update(normalized_dates(str(raw)))
            return
        values = summary_values if role == "query_summary" else direct_values
        if key != "query_summary_source_id":
            values.extend((key, str(raw)))
        if role == "direct":
            if key == "project_id":
                normalized = _canonical_project_id(str(raw))
                if normalized:
                    project_ids.add(normalized)
            if key in _MILESTONE_FIELDS:
                milestones.update(canonical_milestones(str(raw)))
            if key in _PROJECT_STATUS_FIELDS:
                project_statuses.add(str(raw).strip().casefold())
            if key in _OWNERSHIP_FIELDS:
                ownership_values.add(str(raw).strip().casefold())
        if key in _TEMPORAL_FIELDS:
            normalized = normalized_dates(str(raw))
            temporal_values.update(normalized)
            if role == "direct":
                direct_temporal_values.update(normalized)
            elif role == "query_summary":
                summary_temporal_values.update(normalized)
        if key == "age_hours" and role == "direct":
            try:
                age_hours = Decimal(str(raw))
            except InvalidOperation:
                age_hours = Decimal(-1)
            if age_hours >= 0:
                direct_values.extend(("derived_age_days", str(int(age_hours // Decimal(24)))))

    source_role: MaterialRoleV5 = "query_summary" if is_query_summary else "direct"
    source_values = dict(source)
    source_values.update(metadata_raw)
    for key, raw in _iter_safe_fields(source_values, max_depth=1):
        accept(key, raw, role=source_role)
    for material in material_list:
        for key, raw in _iter_safe_fields(material.value):
            accept(key, raw, role=material.role)
        for key, raw in material.value.items():
            if key in {
                "is_query_summary", "query_ordering", "query_scope", "scope_all_projects",
                "current_snapshot", "collection_latest", "query_tool", "query_rank",
                "query_row_count", "query_distinct_project_count", "query_summary_source_id",
            } and isinstance(raw, (str, int, float, bool)):
                accept(key, raw, role="metadata")
        nested_metadata = material.value.get("metadata")
        if isinstance(nested_metadata, dict):
            for key in _METADATA_FIELDS:
                raw = nested_metadata.get(key)
                if isinstance(raw, (str, int, float, bool)) and raw not in (None, ""):
                    accept(key, raw, role=material.role)
    direct_text = " ".join(direct_values)
    summary_text = " ".join(summary_values)
    text = " ".join(part for part in (direct_text, summary_text) if part)
    project_ids.update(_canonical_project_ids(direct_text))
    milestones.update(canonical_milestones(direct_text))
    temporal_values.update(normalized_dates(text))
    dates = frozenset(item for item in temporal_values if item.startswith("date:"))
    direct_scalars = frozenset(normalized_scalars(direct_text))
    summary_scalars = frozenset(normalized_scalars(summary_text))
    direct_tokens = frozenset(claim_tokens(direct_text))
    summary_tokens = frozenset(claim_tokens(summary_text))
    return SourceProjectionV5(
        source_id=source_id,
        kind=str(source.get("kind") or "").strip(),
        text=text,
        tokens=direct_tokens | summary_tokens,
        scalars=direct_scalars,
        dates=dates,
        project_ids=frozenset(project_ids),
        milestones=frozenset(milestones),
        project_statuses=frozenset(project_statuses),
        ownership_values=frozenset(ownership_values),
        temporal_values=frozenset(temporal_values),
        fields=frozenset(fields),
        tools=tools,
        metadata=metadata,
        direct_scalars=direct_scalars,
        query_summary_scalars=summary_scalars,
        direct_tokens=direct_tokens,
        query_summary_tokens=summary_tokens,
        direct_fields=frozenset(direct_fields),
        query_summary_fields=frozenset(summary_fields),
        direct_temporal_values=frozenset(direct_temporal_values),
        query_summary_temporal_values=frozenset(summary_temporal_values),
        direct_text=direct_text,
        query_summary_text=summary_text,
    )


def build_source_projections_v5(
    sources: Iterable[dict[str, Any]],
    tool_results: Iterable[dict[str, Any]] = (),
    *,
    materialize_source_ids: frozenset[str] | None = None,
    cache: ProjectionCacheV5 | None = None,
) -> tuple[dict[str, SourceProjectionV5], int]:
    active_cache = cache or ProjectionCacheV5.build(sources, tool_results)
    selected_ids = active_cache.records if materialize_source_ids is None else materialize_source_ids
    return active_cache.materialize(selected_ids), active_cache.duplicate_source_id_count


__all__ = [
    "AGGREGATE_SOURCE_KINDS", "CURRENT_SOURCE_KINDS", "DATE_RE", "MILESTONE_RE", "NUMBER_RE",
    "PROJECT_ID_RE", "ProjectionCacheV5", "QUERY_SOURCE_KINDS", "RANKING_SOURCE_KINDS",
    "READINESS_SOURCE_KINDS", "ProjectionMaterialV5", "SourceProjectionV5", "build_source_projections_v5",
    "canonical_milestones", "claim_tokens", "normalize_date", "normalize_temporal",
    "normalized_dates", "normalized_scalars",
]
