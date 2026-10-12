"""
Manual Batch Update (Plan Step 3.13c, Blueprint Section 36).
Natural language batch instruction → per-project ProposedUpdate.
Max 20 projects per batch.
"""

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

from src.repositories.project_repo import get_active_projects


MAX_BATCH_SIZE = 20


@dataclass
class BatchItem:
    project_id: str
    client_name: str | None
    changes: dict
    status: str = "pending"  # pending | applied | skipped | error
    error_message: str | None = None


@dataclass
class BatchPreview:
    instruction: str
    items: list[BatchItem]
    total_affected: int
    warnings: list[str] = field(default_factory=list)


def prepare_batch(
    conn: sqlite3.Connection,
    field_name: str,
    new_value: str,
    *,
    project_filter: dict | None = None,
) -> BatchPreview:
    """
    Prepare a batch update preview.
    Validates the field and shows which projects will be affected.
    """
    from config.defaults import WRITABLE_PROJECT_FIELDS

    warnings = []
    if field_name not in WRITABLE_PROJECT_FIELDS:
        warnings.append(f"Field '{field_name}' is not writable.")
        return BatchPreview(
            instruction=f"Set {field_name}={new_value}",
            items=[],
            total_affected=0,
            warnings=warnings,
        )

    projects = get_active_projects(conn)
    if project_filter:
        projects = _apply_filter(projects, project_filter)

    if len(projects) > MAX_BATCH_SIZE:
        warnings.append(
            f"Batch capped at {MAX_BATCH_SIZE} (total matching: {len(projects)})."
        )
        projects = projects[:MAX_BATCH_SIZE]

    items = []
    for p in projects:
        current = getattr(p, field_name, None)
        if current is not None:
            current = str(current.value) if hasattr(current, "value") else str(current)
        if current == new_value:
            continue  # Skip no-ops
        items.append(BatchItem(
            project_id=p.project_id,
            client_name=p.client_name,
            changes={field_name: new_value},
        ))

    return BatchPreview(
        instruction=f"Set {field_name}={new_value}",
        items=items,
        total_affected=len(items),
        warnings=warnings,
    )


def execute_batch(
    conn: sqlite3.Connection,
    preview: BatchPreview,
    *,
    task_type: str = "truth_write_reasoning",
    confidence: float = 0.8,
) -> list[BatchItem]:
    """
    Execute a batch update by submitting writes through safe_write_project.
    Returns the list of items with updated statuses.
    """
    from src.write_layer.write_helpers import safe_write_project

    for item in preview.items:
        if item.status != "pending":
            continue
        try:
            result = safe_write_project(
                conn, item.project_id, item.changes,
                actor="batch",
                reason=f"Batch: {preview.instruction}",
            )
            if result.outcome == "accepted":
                item.status = "applied"
            elif result.outcome == "no_change":
                item.status = "skipped"
                item.error_message = "No change (same value)"
            else:
                item.status = "skipped"
                item.error_message = result.rejection_reason or result.needs_review_reason or "Unknown"
        except Exception as e:
            item.status = "error"
            item.error_message = str(e)

    return preview.items


def format_batch_preview(preview: BatchPreview) -> str:
    """Format batch preview for display."""
    lines = [
        f"Batch Update: {preview.instruction}",
        f"Affected: {preview.total_affected} project(s)",
        "",
    ]
    for w in preview.warnings:
        lines.append(f"WARNING: {w}")
    for item in preview.items[:10]:
        lines.append(f"  {item.project_id} ({item.client_name or '-'})")
    if preview.total_affected > 10:
        lines.append(f"  ... and {preview.total_affected - 10} more")
    return "\n".join(lines)


def save_batch_session(
    conn: sqlite3.Connection,
    session_id: str,
    preview: "BatchPreview",
) -> None:
    """Persist batch preview state into a telegram_sessions row's intermediate_data."""
    import json

    data = {
        "instruction": preview.instruction,
        "items": [
            {
                "project_id": item.project_id,
                "client_name": item.client_name,
                "changes": item.changes,
                "status": item.status,
                "error_message": item.error_message,
            }
            for item in preview.items
        ],
    }
    conn.execute(
        "UPDATE telegram_sessions SET intermediate_data = ? WHERE session_id = ?",
        (json.dumps(data, ensure_ascii=False), session_id),
    )
    conn.commit()


def load_batch_session(
    conn: sqlite3.Connection,
    session_id: str,
) -> "BatchPreview | None":
    """Restore a BatchPreview from a telegram_sessions row."""
    import json

    row = conn.execute(
        "SELECT intermediate_data FROM telegram_sessions WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    if not row or not row["intermediate_data"]:
        return None
    try:
        data = json.loads(row["intermediate_data"])
    except Exception:
        return None
    items = [
        BatchItem(
            project_id=it["project_id"],
            client_name=it.get("client_name"),
            changes=it.get("changes", {}),
            status=it.get("status", "pending"),
            error_message=it.get("error_message"),
        )
        for it in data.get("items", [])
    ]
    return BatchPreview(
        instruction=data.get("instruction", ""),
        items=items,
        total_affected=sum(1 for i in items if i.status == "pending"),
    )


def _apply_filter(projects, filters: dict) -> list:
    """Apply simple filters to project list."""
    result = projects
    for key, value in filters.items():
        result = [
            p for p in result
            if str(getattr(p, key, "")).lower() == str(value).lower()
        ]
    return result
