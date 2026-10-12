"""Shared scope, source, timestamp, and cursor helpers for Workspace read tools."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Callable, Iterable


def workspace_scope(args: dict[str, Any]) -> tuple[str, list[str]]:
    """Return the trusted Workspace scope snapshot injected by the executor."""
    mode = str(args.get("_assistant_scope_mode") or "portfolio")
    raw_ids = args.get("_assistant_scope_project_ids") or []
    project_ids = list(
        dict.fromkeys(str(item).strip() for item in raw_ids if str(item).strip())
    )
    return mode, project_ids


def ensure_project_in_scope(args: dict[str, Any], project_id: str) -> None:
    """Reject access outside a project-scoped Workspace snapshot."""
    mode, project_ids = workspace_scope(args)
    if mode == "projects" and project_id not in project_ids:
        raise PermissionError(
            f"Project {project_id} is outside the active Workspace scope"
        )


def visible_project_ids(
    conn,
    args: dict[str, Any],
    *,
    include_terminal: bool,
) -> list[str]:
    """Resolve visible project IDs without widening a trusted project scope."""
    mode, project_ids = workspace_scope(args)
    if mode == "projects":
        if not project_ids:
            return []
        placeholders = ",".join("?" for _ in project_ids)
        rows = conn.execute(
            f"""SELECT project_id FROM projects
                WHERE project_id IN ({placeholders}) AND project_id != '_system'
                ORDER BY project_id""",
            project_ids,
        ).fetchall()
        found = {str(row[0]) for row in rows}
        return [project_id for project_id in project_ids if project_id in found]

    clauses = ["project_id != '_system'"]
    params: list[Any] = []
    if not include_terminal:
        clauses.append("project_status NOT IN ('Completed','Archived','Cancelled')")
    rows = conn.execute(
        f"SELECT project_id FROM projects WHERE {' AND '.join(clauses)} ORDER BY project_id",
        params,
    ).fetchall()
    return [str(row[0]) for row in rows]


def parse_as_of(value: Any) -> datetime:
    """Parse a trusted snapshot time, defaulting to current UTC."""
    if isinstance(value, datetime):
        current = value
    else:
        raw = str(value or "").strip()
        try:
            current = (
                datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if raw
                else datetime.now(timezone.utc)
            )
        except ValueError:
            current = datetime.now(timezone.utc)
    return current if current.tzinfo else current.replace(tzinfo=timezone.utc)


def normalize_timestamp(value: Any) -> str:
    """Normalize stored SQLite/ISO timestamps to sortable UTC ISO 8601."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _coerce_historical_value(current: Any, old_value: Any) -> Any:
    """Restore an audit value while preserving simple SQLite scalar types."""
    if current is None:
        return None if old_value in (None, "") else old_value
    if isinstance(current, bool):
        return str(old_value).strip().lower() in {"1", "true", "yes"}
    if isinstance(current, int):
        try:
            return int(old_value)
        except (TypeError, ValueError):
            return old_value
    if isinstance(current, float):
        try:
            return float(old_value)
        except (TypeError, ValueError):
            return old_value
    return "" if old_value is None else str(old_value)


def project_row_as_of(
    conn,
    project_id: str,
    as_of: datetime,
) -> dict[str, Any] | None:
    """Rewind accepted project writes so historical reads cannot see future truth."""
    row = conn.execute(
        "SELECT * FROM projects WHERE project_id=? AND project_id != '_system'",
        (project_id,),
    ).fetchone()
    if row is None:
        return None
    project = dict(row)

    as_of = parse_as_of(as_of)
    as_of_utc = as_of.astimezone(timezone.utc).isoformat()
    updated_at = parse_as_of(project.get("updated_at"))
    if updated_at <= as_of:
        project["snapshot_as_of"] = as_of.isoformat()
        project["snapshot_reconstructed"] = False
        project["snapshot_rewound_fields"] = []
        return project

    rows = conn.execute(
        """SELECT proposed_changes, COALESCE(decided_at, created_at) AS effective_at
           FROM proposed_updates
           WHERE project_id=? AND decision_outcome='accepted'
             AND datetime(COALESCE(decided_at, created_at)) > datetime(?)
           ORDER BY datetime(COALESCE(decided_at, created_at)) DESC, rowid DESC""",
        (project_id, as_of_utc),
    ).fetchall()
    rewound_fields: set[str] = set()
    for audit_row in rows:
        try:
            changes = json.loads(audit_row["proposed_changes"] or "[]")
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(changes, list):
            continue
        for change in changes:
            if not isinstance(change, dict):
                continue
            field = str(change.get("field") or "")
            if not field or field not in project:
                continue
            project[field] = _coerce_historical_value(
                project.get(field),
                change.get("old_value"),
            )
            rewound_fields.add(field)

    previous_write = conn.execute(
        """SELECT MAX(COALESCE(decided_at, created_at))
           FROM proposed_updates
           WHERE project_id=? AND decision_outcome='accepted'
             AND datetime(COALESCE(decided_at, created_at)) <= datetime(?)""",
        (project_id, as_of_utc),
    ).fetchone()[0]
    previous_activity = conn.execute(
        """SELECT MAX(created_at) FROM activity_log
           WHERE project_id=? AND datetime(created_at) <= datetime(?)""",
        (project_id, as_of_utc),
    ).fetchone()[0]
    historical_times = [
        str(value) for value in (project.get("created_at"), previous_write) if value
    ]
    project["updated_at"] = max(historical_times, default=as_of_utc)
    project["last_activity_at"] = previous_activity
    project["snapshot_as_of"] = as_of.isoformat()
    project["snapshot_reconstructed"] = bool(rewound_fields)
    project["snapshot_rewound_fields"] = sorted(rewound_fields)
    if not rewound_fields:
        project["snapshot_warning"] = (
            "The current project row was updated after this cutoff and no accepted audit delta "
            "was available to reconstruct mutable fields."
        )
    return project


def parse_time_filter(value: Any, *, end_of_day: bool = False) -> str | None:
    """Parse an ISO date/timestamp filter into a UTC-comparable timestamp."""
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Invalid ISO date/time: {raw}") from exc
    if len(raw) == 10 and end_of_day:
        parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def source_id(kind: str, locator: str) -> str:
    digest = hashlib.sha1(f"{kind}:{locator}".encode("utf-8")).hexdigest()[:12]
    return f"{kind}-{digest}"


def add_source(
    args: dict[str, Any],
    *,
    kind: str,
    title: str,
    locator: str,
    excerpt: str = "",
    project_id: str | None = None,
    occurred_at: str | None = None,
    url: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    """Append a deduplicated, renderable source record to the trusted sink."""
    record_id = source_id(kind, locator)
    sink = args.get("_assistant_source_sink")
    record = {
        "type": "source",
        "source_id": record_id,
        "kind": kind,
        "title": title,
        "locator": locator,
        "snippet": excerpt,
        "project_id": project_id,
        "occurred_at": occurred_at,
        "url": url,
        "metadata": metadata or {},
    }
    if isinstance(sink, list) and not any(
        item.get("source_id") == record_id for item in sink
    ):
        sink.append(record)
    return record_id


def encode_cursor(values: Iterable[Any]) -> str:
    payload = json.dumps(
        {"v": 1, "keys": [str(value or "") for value in values]},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_cursor(cursor: str | None) -> tuple[str, ...] | None:
    if not cursor:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        if payload.get("v") != 1 or not isinstance(payload.get("keys"), list):
            raise ValueError
        return tuple(str(value) for value in payload["keys"])
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("Invalid cursor") from exc


def paginate_records(
    records: list[dict[str, Any]],
    *,
    key: Callable[[dict[str, Any]], tuple[str, ...]],
    cursor: str | None,
    limit: int,
    descending: bool,
) -> tuple[list[dict[str, Any]], str | None]:
    """Apply deterministic keyset pagination to normalized in-memory records."""
    ordered = sorted(records, key=key, reverse=descending)
    cursor_key = decode_cursor(cursor)
    if cursor_key is not None:
        if descending:
            ordered = [item for item in ordered if key(item) < cursor_key]
        else:
            ordered = [item for item in ordered if key(item) > cursor_key]
    page = ordered[:limit]
    next_cursor = None
    if len(ordered) > limit and page:
        next_cursor = encode_cursor(key(page[-1]))
    return page, next_cursor
