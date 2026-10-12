"""Strict purpose-sized portfolio query used by unified Agent read serving.

The broad Agent tool registry intentionally does not expose this internal capability.
It accepts a small typed information request, enforces trusted scope, and returns
only the data required for that operation.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import sqlite3
from datetime import date, datetime, time, timezone
from typing import Any, Literal
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.assistant.portfolio_query_v2_renderer import render_portfolio_query_v2
from src.assistant.workspace_read_support import add_source, parse_as_of, project_row_as_of
from src.models.enums import BallWith, Milestone, ProjectStatus

Operation = Literal["count", "list", "exists", "group"]
GroupField = Literal["project_status", "current_milestone", "ball_with", "market"]
TransitionField = Literal["project_status"]
ProjectField = Literal[
    "project_id",
    "client_name",
    "project_status",
    "current_milestone",
    "ball_with",
    "market",
    "next_action",
    "target_go_live",
    "updated_at",
    "transitioned_at",
]
SortDirection = Literal["asc", "desc"]

_TERMINAL_STATUSES = frozenset(item.value for item in ProjectStatus.terminal_statuses())
PORTFOLIO_PROJECT_DETAIL_FIELDS: tuple[str, ...] = (
    "project_id",
    "client_name",
    "project_status",
    "current_milestone",
    "ball_with",
    "market",
    "next_action",
    "target_go_live",
)
_DEFAULT_LIST_FIELDS = PORTFOLIO_PROJECT_DETAIL_FIELDS
_ALLOWED_FIELDS = frozenset(ProjectField.__args__)
_ALLOWED_GROUPS = frozenset(GroupField.__args__)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class PortfolioTransitionFilterV2(BaseModel):
    """Accepted project-field transition filter backed by the write audit trail."""

    model_config = ConfigDict(extra="forbid")

    field: TransitionField
    to_values: list[str] = Field(min_length=1)
    effective_from: str | None = None
    effective_to: str | None = None

    @field_validator("to_values")
    @classmethod
    def validate_to_values(cls, values: list[str]) -> list[str]:
        return _canonical_values(
            values, [item.value for item in ProjectStatus], "transition.to_values"
        )

    @field_validator("effective_from", "effective_to")
    @classmethod
    def validate_effective_dates(cls, value: str | None) -> str | None:
        normalized = str(value or "").strip()
        if not normalized:
            return None
        if not _DATE_RE.fullmatch(normalized):
            raise ValueError("transition effective dates must use YYYY-MM-DD")
        date.fromisoformat(normalized)
        return normalized

    @model_validator(mode="after")
    def validate_effective_range(self) -> "PortfolioTransitionFilterV2":
        if (
            self.effective_from
            and self.effective_to
            and self.effective_from > self.effective_to
        ):
            raise ValueError("transition.effective_from cannot be later than transition.effective_to")
        return self


class PortfolioFiltersV2(BaseModel):
    """Allowlisted project filters; lists are OR within a field and AND across fields."""

    model_config = ConfigDict(extra="forbid")

    project_id: list[str] = Field(default_factory=list)
    project_status: list[str] = Field(default_factory=list)
    current_milestone: list[str] = Field(default_factory=list)
    ball_with: list[str] = Field(default_factory=list)
    market: list[str] = Field(default_factory=list)
    client_name: str | None = None
    target_go_live_from: str | None = None
    target_go_live_to: str | None = None
    transition: PortfolioTransitionFilterV2 | None = None

    @field_validator("project_id")
    @classmethod
    def normalize_project_ids(cls, values: list[str]) -> list[str]:
        if len(values) > 100:
            raise ValueError("project_id accepts at most 100 identifiers")
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            project_id = str(value).strip()
            if not project_id:
                continue
            if len(project_id) > 128 or any(ord(char) < 32 for char in project_id):
                raise ValueError("project_id values must be printable and at most 128 characters")
            canonical = project_id.upper()
            key = canonical.casefold()
            if key in seen:
                continue
            seen.add(key)
            normalized.append(canonical)
        return normalized

    @field_validator("project_status")
    @classmethod
    def validate_statuses(cls, values: list[str]) -> list[str]:
        return _canonical_values(values, [item.value for item in ProjectStatus], "project_status")

    @field_validator("current_milestone")
    @classmethod
    def validate_milestones(cls, values: list[str]) -> list[str]:
        canonical = [item.value for item in Milestone]
        aliases = {f"M{index}": value for index, value in enumerate(canonical, start=1)}
        return _canonical_values(values, canonical, "current_milestone", aliases=aliases)

    @field_validator("ball_with")
    @classmethod
    def validate_ball_with(cls, values: list[str]) -> list[str]:
        return _canonical_values(values, [item.value for item in BallWith], "ball_with")

    @field_validator("market")
    @classmethod
    def normalize_markets(cls, values: list[str]) -> list[str]:
        normalized = [str(value).strip().upper() for value in values if str(value).strip()]
        return list(dict.fromkeys(normalized))

    @field_validator("client_name")
    @classmethod
    def normalize_client_name(cls, value: str | None) -> str | None:
        normalized = str(value or "").strip()
        return normalized or None

    @field_validator("target_go_live_from", "target_go_live_to")
    @classmethod
    def validate_dates(cls, value: str | None) -> str | None:
        normalized = str(value or "").strip()
        if not normalized:
            return None
        if not _DATE_RE.fullmatch(normalized):
            raise ValueError("target go-live filters must use YYYY-MM-DD")
        date.fromisoformat(normalized)
        return normalized

    @model_validator(mode="after")
    def validate_date_range(self) -> "PortfolioFiltersV2":
        if (
            self.target_go_live_from
            and self.target_go_live_to
            and self.target_go_live_from > self.target_go_live_to
        ):
            raise ValueError("target_go_live_from cannot be later than target_go_live_to")
        return self


class PortfolioSortV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    direction: SortDirection = "asc"

    @field_validator("field")
    @classmethod
    def validate_field(cls, value: str) -> str:
        if value not in _ALLOWED_FIELDS:
            raise ValueError(f"Unsupported sort field: {value}")
        return value


class PortfolioQueryV2Request(BaseModel):
    """Strict internal contract for bounded portfolio reads."""

    model_config = ConfigDict(extra="forbid")

    operation: Operation
    filters: PortfolioFiltersV2 = Field(default_factory=PortfolioFiltersV2)
    include_terminal: bool = False
    group_by: str | None = None
    fields: list[str] = Field(default_factory=list)
    sort: list[PortfolioSortV2] = Field(default_factory=list)
    limit: int = Field(default=50, ge=1, le=100)
    cursor: str | None = None

    @field_validator("group_by")
    @classmethod
    def validate_group_by(cls, value: str | None) -> str | None:
        if value is not None and value not in _ALLOWED_GROUPS:
            raise ValueError(f"Unsupported group field: {value}")
        return value

    @field_validator("fields")
    @classmethod
    def validate_fields(cls, values: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))
        unknown = set(normalized) - _ALLOWED_FIELDS
        if unknown:
            raise ValueError(f"Unsupported output fields: {sorted(unknown)}")
        return normalized

    @model_validator(mode="after")
    def validate_operation_shape(self) -> "PortfolioQueryV2Request":
        if self.operation == "group" and not self.group_by:
            raise ValueError("group operation requires group_by")
        if self.operation != "group" and self.group_by is not None:
            raise ValueError("group_by is only valid for group operation")
        if self.operation != "list" and (self.fields or self.sort or self.cursor):
            raise ValueError("fields, sort, and cursor are only valid for list operation")
        return self


def portfolio_query_v2(
    conn: sqlite3.Connection,
    args: dict[str, Any],
) -> dict[str, Any]:
    """Execute a purpose-sized portfolio query against a trusted Workspace snapshot."""
    request = PortfolioQueryV2Request.model_validate(_public_args(args))
    scope_mode, trusted_project_ids = _trusted_scope(args)
    as_of = parse_as_of(args.get("_assistant_as_of"))
    snapshot_rows = _snapshot_rows(conn, as_of, scope_mode, trusted_project_ids)
    if request.filters.transition is not None:
        transition_matches = _accepted_transition_matches(
            conn,
            project_ids=[str(row["project_id"]) for row in snapshot_rows],
            transition=request.filters.transition,
            as_of=as_of,
        )
        snapshot_rows = [
            {**row, **transition_matches[str(row["project_id"])]}
            for row in snapshot_rows
            if str(row["project_id"]) in transition_matches
        ]
    requested_include_terminal = bool(request.include_terminal)
    rows = [
        row
        for row in snapshot_rows
        if _matches(row, request.filters, requested_include_terminal)
    ]
    effective_include_terminal = _effective_include_terminal(
        request,
        matched_rows=rows,
    )
    fingerprint = _query_fingerprint(
        request,
        scope_mode,
        trusted_project_ids,
        as_of,
        effective_include_terminal=effective_include_terminal,
    )
    summary_source_id = _summary_source(
        args,
        request=request,
        scope_mode=scope_mode,
        project_ids=trusted_project_ids,
        as_of=as_of,
        total=len(rows),
        fingerprint=fingerprint,
        effective_include_terminal=effective_include_terminal,
    )
    selected_project_ids = sorted(str(row["project_id"]) for row in rows)
    base = {
        "schema_version": 2,
        "operation": request.operation,
        "as_of": as_of.isoformat(),
        "matched_project_count": len(rows),
        "selected_project_signature": hashlib.sha256(
            json.dumps(selected_project_ids, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "effective_filters": request.filters.model_dump(mode="json", exclude_none=True),
        "requested_include_terminal": requested_include_terminal,
        "effective_include_terminal": effective_include_terminal,
        "effective_scope": {
            "mode": scope_mode,
            "project_ids": trusted_project_ids if scope_mode == "projects" else [],
        },
        "summary_source_id": summary_source_id,
    }

    if request.operation == "count":
        return {**base, "value": len(rows), "is_complete": True}
    if request.operation == "exists":
        return {**base, "value": bool(rows), "matched_count": len(rows), "is_complete": True}
    if request.operation == "group":
        groups = _group_rows(rows, str(request.group_by))
        return {
            **base,
            "group_by": request.group_by,
            "groups": groups,
            "matched_project_count": len(rows),
            "group_membership_mode": (
                "token_membership" if request.group_by == "market" else "single_value"
            ),
            "is_complete": True,
        }

    fields = tuple(request.fields or _DEFAULT_LIST_FIELDS)
    if request.filters.transition is not None and "transitioned_at" not in fields:
        fields = (*fields, "transitioned_at")
    sort = request.sort or [PortfolioSortV2(field="project_id", direction="asc")]
    ordered = _sort_rows(rows, sort)
    offset = _decode_cursor(request.cursor, fingerprint)
    page = ordered[offset : offset + request.limit]
    next_offset = offset + len(page)
    next_cursor = (
        _encode_cursor(next_offset, fingerprint) if next_offset < len(ordered) else None
    )
    results = [
        _project_result(args, row, fields=fields, as_of=as_of)
        for row in page
    ]
    return {
        **base,
        "fields": list(fields),
        "sort": [item.model_dump(mode="json") for item in sort],
        "total": len(ordered),
        "returned": len(results),
        "results": results,
        "next_cursor": next_cursor,
        "is_complete": next_cursor is None,
    }



def _public_args(args: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in args.items() if not str(key).startswith("_assistant_")}


def _canonical_values(
    values: list[str],
    canonical: list[str],
    field: str,
    *,
    aliases: dict[str, str] | None = None,
) -> list[str]:
    lookup = {item.casefold(): item for item in canonical}
    alias_lookup = {key.casefold(): value for key, value in (aliases or {}).items()}
    output: list[str] = []
    for raw in values:
        value = str(raw).strip()
        selected = lookup.get(value.casefold()) or alias_lookup.get(value.casefold())
        if selected is None:
            raise ValueError(f"Unsupported {field} value: {value}")
        if selected not in output:
            output.append(selected)
    return output


def _trusted_scope(args: dict[str, Any]) -> tuple[str, list[str]]:
    mode = str(args.get("_assistant_scope_mode") or "portfolio")
    if mode not in {"portfolio", "projects"}:
        raise ValueError(f"Unsupported Workspace scope: {mode}")
    ids = list(
        dict.fromkeys(
            str(item).strip()
            for item in (args.get("_assistant_scope_project_ids") or [])
            if str(item).strip()
        )
    )
    return mode, ids


def _snapshot_rows(
    conn: sqlite3.Connection,
    as_of: datetime,
    scope_mode: str,
    project_ids: list[str],
) -> list[dict[str, Any]]:
    if scope_mode == "projects":
        candidates = project_ids
    else:
        candidates = [
            str(row[0])
            for row in conn.execute(
                "SELECT project_id FROM projects WHERE project_id != ? ORDER BY project_id",
                ("_system",),
            ).fetchall()
        ]
    rows: list[dict[str, Any]] = []
    for project_id in candidates:
        row = project_row_as_of(conn, project_id, as_of)
        if row is not None:
            rows.append(row)
    return rows


def _accepted_transition_matches(
    conn: sqlite3.Connection,
    *,
    project_ids: list[str],
    transition: PortfolioTransitionFilterV2,
    as_of: datetime,
) -> dict[str, dict[str, Any]]:
    """Return the latest accepted matching transition for each trusted project."""
    if not project_ids:
        return {}
    placeholders = ",".join("?" for _ in project_ids)
    upper_bound = min(
        as_of.astimezone(timezone.utc),
        _date_bound(transition.effective_to, end_of_day=True) or as_of.astimezone(timezone.utc),
    )
    lower_bound = _date_bound(transition.effective_from, end_of_day=False)
    params: list[Any] = [*project_ids, upper_bound.isoformat()]
    sql = f"""SELECT update_id, project_id, proposed_changes, source_type, source_ref,
                     COALESCE(decided_at, created_at) AS effective_at
              FROM proposed_updates
              WHERE project_id IN ({placeholders})
                AND decision_outcome='accepted'
                AND datetime(COALESCE(decided_at, created_at)) <= datetime(?)"""
    if lower_bound is not None:
        sql += " AND datetime(COALESCE(decided_at, created_at)) >= datetime(?)"
        params.append(lower_bound.isoformat())
    sql += " ORDER BY datetime(COALESCE(decided_at, created_at)) DESC, rowid DESC"
    matches: dict[str, dict[str, Any]] = {}
    expected = set(transition.to_values)
    for audit_row in conn.execute(sql, params).fetchall():
        project_id = str(audit_row["project_id"] or "")
        if not project_id or project_id in matches:
            continue
        try:
            changes = json.loads(audit_row["proposed_changes"] or "[]")
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(changes, list):
            continue
        for change in changes:
            if not isinstance(change, dict):
                continue
            if str(change.get("field") or "") != transition.field:
                continue
            new_value = str(change.get("new_value") or "")
            if new_value not in expected:
                continue
            matches[project_id] = {
                "transitioned_at": str(audit_row["effective_at"] or ""),
                "transition_update_id": str(audit_row["update_id"] or ""),
                "transition_field": transition.field,
                "transition_old_value": change.get("old_value"),
                "transition_new_value": new_value,
                "transition_source_type": audit_row["source_type"],
                "transition_source_ref": audit_row["source_ref"],
            }
            break
    return matches


def _date_bound(value: str | None, *, end_of_day: bool) -> datetime | None:
    if not value:
        return None
    parsed = date.fromisoformat(value)
    boundary = time.max if end_of_day else time.min
    return datetime.combine(parsed, boundary, tzinfo=timezone.utc)


def _market_tokens(value: Any) -> set[str]:
    return {
        token.strip().upper()
        for token in re.split(r"[,;/|]+", str(value or ""))
        if token.strip()
    }


def _matches(row: dict[str, Any], filters: PortfolioFiltersV2, include_terminal: bool) -> bool:
    if filters.project_id:
        actual_project_id = str(row.get("project_id") or "").casefold()
        if actual_project_id not in {item.casefold() for item in filters.project_id}:
            return False
    if not include_terminal and not filters.project_status and filters.transition is None:
        if str(row.get("project_status") or "") in _TERMINAL_STATUSES:
            return False
    for field in ("project_status", "current_milestone", "ball_with"):
        expected = getattr(filters, field)
        if expected and str(row.get(field) or "") not in expected:
            return False
    if filters.market:
        if not set(filters.market).intersection(_market_tokens(row.get("market"))):
            return False
    if filters.client_name:
        if filters.client_name.casefold() not in str(row.get("client_name") or "").casefold():
            return False
    target = str(row.get("target_go_live") or "").strip()[:10]
    if filters.target_go_live_from and (not target or target < filters.target_go_live_from):
        return False
    if filters.target_go_live_to and (not target or target > filters.target_go_live_to):
        return False
    return True


def _group_rows(rows: list[dict[str, Any]], group_by: str) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for row in rows:
        keys = sorted(_market_tokens(row.get("market"))) if group_by == "market" else [str(row.get(group_by) or "")]
        for key in keys or [""]:
            counts[key] = counts.get(key, 0) + 1
    return [{"key": key, "count": counts[key]} for key in sorted(counts, key=lambda item: (item == "", item.casefold()))]


def _sort_rows(rows: list[dict[str, Any]], sort: list[PortfolioSortV2]) -> list[dict[str, Any]]:
    ordered = list(rows)
    if not any(item.field == "project_id" for item in sort):
        sort = [*sort, PortfolioSortV2(field="project_id", direction="asc")]
    for item in reversed(sort):
        ordered.sort(
            key=lambda row: str(row.get(item.field) or "").casefold(),
            reverse=item.direction == "desc",
        )
    return ordered


def _effective_include_terminal(
    request: PortfolioQueryV2Request,
    *,
    matched_rows: list[dict[str, Any]],
) -> bool:
    """Return whether terminal inclusion materially changed the selected evidence."""
    if (
        not request.include_terminal
        or request.filters.project_status
        or request.filters.transition is not None
    ):
        return False
    return any(
        str(row.get("project_status") or "") in _TERMINAL_STATUSES
        for row in matched_rows
    )


def _query_fingerprint(
    request: PortfolioQueryV2Request,
    scope_mode: str,
    project_ids: list[str],
    as_of: datetime,
    *,
    effective_include_terminal: bool,
) -> str:
    payload = {
        "selection": {
            "operation": request.operation,
            "filters": request.filters.model_dump(mode="json"),
            "include_terminal": effective_include_terminal,
            "group_by": request.group_by,
        },
        "scope_mode": scope_mode,
        "project_ids": project_ids,
        "as_of": as_of.isoformat(),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:20]


def _encode_cursor(offset: int, fingerprint: str) -> str:
    raw = json.dumps({"v": 2, "offset": offset, "fingerprint": fingerprint}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str | None, fingerprint: str) -> int:
    if not cursor:
        return 0
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode())
        if payload.get("v") != 2 or payload.get("fingerprint") != fingerprint:
            raise ValueError
        offset = int(payload["offset"])
        if offset < 0:
            raise ValueError
        return offset
    except (ValueError, TypeError, KeyError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("Invalid or stale portfolio_query_v2 cursor") from exc


def _summary_source(
    args: dict[str, Any],
    *,
    request: PortfolioQueryV2Request,
    scope_mode: str,
    project_ids: list[str],
    as_of: datetime,
    total: int,
    fingerprint: str,
    effective_include_terminal: bool,
) -> str:
    return add_source(
        args,
        kind="portfolio_query_v2",
        title="Purpose-sized portfolio query summary",
        locator=f"portfolio-query-v2:{fingerprint}",
        occurred_at=as_of.isoformat(),
        excerpt=f"The typed {request.operation} query matched {total} projects in the trusted {scope_mode} scope.",
        metadata={
            "operation": request.operation,
            "filters": request.filters.model_dump(mode="json", exclude_none=True),
            "include_terminal": effective_include_terminal,
            "group_by": request.group_by,
            "scope_mode": scope_mode,
            "scope_project_ids": project_ids if scope_mode == "projects" else [],
            "matched_project_count": total,
            "as_of": as_of.isoformat(),
        },
    )


def _project_result(
    args: dict[str, Any],
    row: dict[str, Any],
    *,
    fields: tuple[str, ...],
    as_of: datetime,
) -> dict[str, Any]:
    project_id = str(row["project_id"])
    selected = {field: row.get(field) for field in fields}
    selected.setdefault("project_id", project_id)
    transition_update_id = str(row.get("transition_update_id") or "")
    if transition_update_id:
        source = add_source(
            args,
            kind="project_status_transition",
            title=f"{project_id} accepted status transition",
            locator=f"proposed-update:{transition_update_id}",
            project_id=project_id,
            occurred_at=str(row.get("transitioned_at") or as_of.isoformat()),
            url=f"/projects/{quote(project_id)}",
            excerpt=(
                f"{row.get('transition_field')} changed from "
                f"{row.get('transition_old_value')} to {row.get('transition_new_value')} "
                f"at {row.get('transitioned_at')}; "
                + "; ".join(f"{key}={value}" for key, value in selected.items())
            ),
            metadata={
                "snapshot_as_of": as_of.isoformat(),
                "fields": list(fields),
                "update_id": transition_update_id,
                "transition_field": row.get("transition_field"),
                "old_value": row.get("transition_old_value"),
                "new_value": row.get("transition_new_value"),
                "source_type": row.get("transition_source_type"),
                "source_ref": row.get("transition_source_ref"),
            },
        )
        return {
            **selected,
            "transition_update_id": transition_update_id,
            "transition_source_id": source,
            "source_id": source,
        }
    source = add_source(
        args,
        kind="project",
        title=f"{project_id} project record",
        locator=f"project:{project_id}:{row.get('updated_at') or ''}",
        project_id=project_id,
        occurred_at=str(row.get("updated_at") or as_of.isoformat()),
        url=f"/projects/{quote(project_id)}",
        excerpt="; ".join(f"{key}={value}" for key, value in selected.items()),
        metadata={"snapshot_as_of": as_of.isoformat(), "fields": list(fields)},
    )
    return {**selected, "source_id": source}



PORTFOLIO_QUERY_V2_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "operation": {"type": "string", "enum": ["count", "list", "exists", "group"]},
        "filters": {
            "type": "object",
            "properties": {
                "project_id": {"type": "array", "maxItems": 100, "items": {"type": "string", "minLength": 1, "maxLength": 128}},
                "project_status": {"type": "array", "items": {"type": "string", "enum": [item.value for item in ProjectStatus]}},
                "current_milestone": {"type": "array", "items": {"type": "string", "enum": [item.value for item in Milestone]}},
                "ball_with": {"type": "array", "items": {"type": "string", "enum": [item.value for item in BallWith]}},
                "market": {"type": "array", "items": {"type": "string"}},
                "client_name": {"type": ["string", "null"]},
                "target_go_live_from": {"type": ["string", "null"]},
                "target_go_live_to": {"type": ["string", "null"]},
                "transition": {
                    "anyOf": [
                        {
                            "type": "object",
                            "properties": {
                                "field": {"type": "string", "enum": ["project_status"]},
                                "to_values": {
                                    "type": "array",
                                    "minItems": 1,
                                    "items": {"type": "string", "enum": [item.value for item in ProjectStatus]},
                                },
                                "effective_from": {"type": ["string", "null"]},
                                "effective_to": {"type": ["string", "null"]},
                            },
                            "required": ["field", "to_values", "effective_from", "effective_to"],
                            "additionalProperties": False,
                        },
                        {"type": "null"},
                    ]
                },
            },
            "required": ["project_id", "project_status", "current_milestone", "ball_with", "market", "client_name", "target_go_live_from", "target_go_live_to", "transition"],
            "additionalProperties": False,
        },
        "include_terminal": {"type": "boolean"},
        "group_by": {"type": ["string", "null"], "enum": ["project_status", "current_milestone", "ball_with", "market", None]},
        "fields": {"type": "array", "items": {"type": "string", "enum": sorted(_ALLOWED_FIELDS)}},
        "sort": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string", "enum": sorted(_ALLOWED_FIELDS)},
                    "direction": {"type": "string", "enum": ["asc", "desc"]},
                },
                "required": ["field", "direction"],
                "additionalProperties": False,
            },
        },
        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        "cursor": {"type": ["string", "null"]},
    },
    "required": ["operation", "filters", "include_terminal", "group_by", "fields", "sort", "limit", "cursor"],
    "additionalProperties": False,
}


__all__ = [
    "PORTFOLIO_PROJECT_DETAIL_FIELDS",
    "PORTFOLIO_QUERY_V2_JSON_SCHEMA",
    "PortfolioFiltersV2",
    "PortfolioTransitionFilterV2",
    "PortfolioQueryV2Request",
    "PortfolioSortV2",
    "portfolio_query_v2",
    "render_portfolio_query_v2",
]
