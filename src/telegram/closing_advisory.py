"""Read-only closing guidance shared by Telegram and web chat."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

from config.settings import CONFIG
from src.portfolio.project_closing import ClosingStatus, get_closing_status


def dashboard_closing_url(project_id: str) -> str:
    """Return the canonical Dashboard closing review URL."""
    base = (CONFIG.dashboard_base_url or "http://127.0.0.1:3317").rstrip("/")
    return f"{base}/projects/{project_id}/closing"


@contextmanager
def _read_conn(conn: sqlite3.Connection | None) -> Iterator[sqlite3.Connection]:
    if conn is not None:
        yield conn
        return
    from src.db.connection import scoped_read

    with scoped_read() as read_conn:
        yield read_conn


def build_closing_advisory(
    conn: sqlite3.Connection | None,
    project_id: str,
    *,
    reopen: bool = False,
) -> str:
    """Render project closing readiness without mutating project state."""
    with _read_conn(conn) as read_conn:
        status = get_closing_status(read_conn, project_id)

    url = dashboard_closing_url(project_id)
    if status is None:
        return f"\u627e\u4e0d\u5230 **{project_id}**\u3002\u8acb\u5728 Dashboard \u6aa2\u67e5 project ID\uff1a{url}"
    if reopen:
        return _render_reopen_advisory(status, url)
    return _render_close_advisory(status, url)


def _render_close_advisory(status: ClosingStatus, url: str) -> str:
    resolved = sum(item.status in {"confirmed", "dismissed"} for item in status.items)
    total = len(status.items)
    milestone = status.current_milestone or "\u672a\u8a2d\u5b9a"
    lines = [
        f"**{status.project_id} \u7d50\u6848\u6aa2\u8996**",
        f"- \u72c0\u614b\uff1a{status.project_status}",
        f"- Milestone\uff1a{milestone}",
        f"- Checklist\uff1a{resolved}/{total} \u5df2\u8655\u7406",
    ]

    if status.closed_at:
        lines.append(
            f"- \u5df2\u7531 {status.signed_off_by or 'operator'} \u65bc {status.closed_at} \u5b8c\u6210\u7d50\u6848"
        )
    elif status.can_close:
        lines.append("- Readiness\uff1a\u5df2\u7b26\u5408 final sign-off \u689d\u4ef6")
    else:
        lines.append("- Readiness\uff1a\u5c1a\u672a\u7b26\u5408 final sign-off \u689d\u4ef6")

    if status.blockers:
        lines.append("\n**\u963b\u64cb\u9805\u76ee**")
        lines.extend(f"- {issue.message}" for issue in status.blockers)
    if status.warnings:
        lines.append("\n**\u9700\u78ba\u8a8d\u4e8b\u9805**")
        lines.extend(f"- {issue.message}" for issue in status.warnings)

    lines.extend([
        "",
        "Telegram\uff0fWeb Chat \u53ea\u63d0\u4f9b readiness\uff1b\u4e0d\u6703\u5efa\u7acb pending update\uff0c\u4e5f\u4e0d\u6703\u8b8a\u66f4 project status\u3002",
        "\u6700\u7d42\u7d50\u6848\u5fc5\u9808\u7531\u5177\u5099 `project.close` \u6b0a\u9650\u7684 operator \u5728 Dashboard \u5b8c\u6210 checklist\u3001warning acknowledgement \u8207 final sign-off\u3002",
        f"Dashboard\uff1a{url}",
    ])
    return "\n".join(lines)


def _render_reopen_advisory(status: ClosingStatus, url: str) -> str:
    if status.project_status == "Completed":
        state = "\u76ee\u524d\u5df2 Completed\uff1breopen \u5fc5\u9808\u7531 Dashboard operator \u63d0\u4f9b\u539f\u56e0\u4e26\u57f7\u884c\u3002"
    else:
        state = f"\u76ee\u524d\u72c0\u614b\u70ba {status.project_status}\uff0c\u4e0d\u662f Completed\u3002"
    return "\n".join([
        f"**{status.project_id} Reopen \u6aa2\u8996**",
        state,
        "Telegram\uff0fWeb Chat \u4e0d\u6703\u8b8a\u66f4 project status\u3002",
        f"Dashboard\uff1a{url}",
    ])
