"""Deterministic renderer for portfolio_query_v2 typed results."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo

Presentation = Literal["workspace", "telegram"]

_HONG_KONG = ZoneInfo("Asia/Hong_Kong")
_STATUS_LABELS_ZH = {
    "Active": "\u9032\u884c\u4e2d",
    "Blocked": "\u53d7\u963b",
    "Waiting": "\u7b49\u5f85\u4e2d",
    "Completed": "\u5df2\u5b8c\u6210",
    "Archived": "\u5df2\u5c01\u5b58",
    "Cancelled": "\u5df2\u53d6\u6d88",
}
_BALL_WITH_LABELS_ZH = {
    "Internal": "\u5167\u90e8",
    "External": "\u5916\u90e8",
    "Shared": "\u96d9\u65b9",
    "Unknown": "\u672a\u77e5",
}


def render_portfolio_query_v2(
    result: dict[str, Any],
    *,
    language: Literal["en", "zh-Hant"] = "en",
    presentation: Presentation = "workspace",
) -> str:
    """Render a typed result without interpreting the original question wording."""
    if presentation == "telegram":
        return _render_telegram(result, language=language)
    return _render_workspace(result, language=language)


def _render_workspace(result: dict[str, Any], *, language: str) -> str:
    """Preserve citation-rich canonical output used by Workspace."""
    source_id = str(result["summary_source_id"])
    as_of = str(result.get("as_of") or "")
    scope = _render_scope(result, language=language)
    effective_filters = result.get("effective_filters") or {}
    filters = _render_filters(effective_filters, language=language)
    separator = "\uff1b" if language == "zh-Hant" else "; "
    qualifier = separator.join(item for item in (scope, filters) if item)
    citation = f"[[source:{source_id}]]"
    operation = result.get("operation")
    transition = effective_filters.get("transition")

    if operation == "count" and isinstance(transition, dict):
        return _render_workspace_transition_count(
            result, transition, language=language, citation=citation
        )

    if language == "zh-Hant":
        if operation == "count":
            return f"\u622a\u81f3 {as_of}\uff0c\u7b26\u5408\u689d\u4ef6\u7684\u5c08\u6848\u5171\u6709 **{int(result['value'])}** \u500b\uff08{qualifier}\uff09\u3002 {citation}"
        if operation == "exists":
            answer = "\u6709" if result.get("value") else "\u6c92\u6709"
            return f"\u622a\u81f3 {as_of}\uff0c{answer}\u7b26\u5408\u689d\u4ef6\u7684\u5c08\u6848\uff08{qualifier}\uff09\u3002 {citation}"
        if operation == "group":
            lines = [f"\u622a\u81f3 {as_of}\uff0c\u5206\u7d44\u7d50\u679c\u5982\u4e0b\uff08{qualifier}\uff09\uff1a {citation}"]
            for item in result.get("groups", []):
                key = item["key"] or "\u672a\u8a2d\u5b9a"
                lines.append(f"- {key}\uff1a{item['count']}")
            return "\n".join(lines)
        lines = [f"\u622a\u81f3 {as_of}\uff0c\u5171\u627e\u5230 **{int(result.get('total') or 0)}** \u500b\u5c08\u6848\uff08{qualifier}\uff09\u3002 {citation}"]
    else:
        if operation == "count":
            return f"As of {as_of}, **{int(result['value'])}** projects match ({qualifier}). {citation}"
        if operation == "exists":
            answer = "Yes" if result.get("value") else "No"
            existence = "exists" if result.get("value") else "does not exist"
            return f"As of {as_of}, **{answer}** - a matching project {existence} ({qualifier}). {citation}"
        if operation == "group":
            lines = [f"As of {as_of}, the grouped result is ({qualifier}): {citation}"]
            lines.extend(f"- {item['key'] or 'Not set'}: {item['count']}" for item in result.get("groups", []))
            return "\n".join(lines)
        lines = [f"As of {as_of}, **{int(result.get('total') or 0)}** projects match ({qualifier}). {citation}"]

    for row in result.get("results") or []:
        row_citation = f"[[source:{row['source_id']}]]"
        missing = "\u672a\u8a2d\u5b9a" if language == "zh-Hant" else "?"
        values = [
            f"{_field_label(key, language=language)}="
            f"{row.get(key) if row.get(key) not in (None, '') else missing}"
            for key in result.get("fields") or []
            if key != "project_id"
        ]
        project = row.get("project_id") or ("\u5c08\u6848" if language == "zh-Hant" else "Project")
        if language == "zh-Hant":
            details = "\u3001".join(values)
            lines.append(f"- **{project}** \u2014 {details}\u3002 {row_citation}")
        else:
            lines.append(f"- **{project}** - {', '.join(values)}. {row_citation}")
    if result.get("next_cursor"):
        lines.append(
            "- \u4e0b\u4e00\u9801\u4ecd\u6709\u66f4\u591a\u7b26\u5408\u689d\u4ef6\u7684\u5c08\u6848\u3002"
            if language == "zh-Hant"
            else "- More matching projects are available on the next page."
        )
    return "\n".join(lines)


def _render_workspace_transition_count(
    result: dict[str, Any],
    transition: dict[str, Any],
    *,
    language: str,
    citation: str,
) -> str:
    count = int(result.get("value") or 0)
    field = str(transition.get("field") or "project_status")
    values = list(transition.get("to_values") or [])
    target = _joined_values(field, values, language)
    period = _period_description(result, transition, language)
    completed = field == "project_status" and {str(value) for value in values} == {"Completed"}
    if language == "zh-Hant":
        headline = (
            f"{period}\u5df2\u5b8c\u6210 **{count}** \u500b\u5c08\u6848\u3002 {citation}"
            if completed
            else f"{period}\u6709 **{count}** \u500b\u5c08\u6848\u8f49\u70ba\u300c{target}\u300d\u3002 {citation}"
        )
        lines = [headline, "", f"\u7d71\u8a08\u4f9d\u64da\uff1a\u5df2\u63a5\u53d7\u7684\u300c{_field_label(field, language=language)}\u8f49\u70ba{target}\u300d\u5be9\u6838\u7d00\u9304\u3002"]
    else:
        action = "were completed" if completed else f"changed to {target}"
        lines = [
            f"**{count}** projects {action} {period}. {citation}",
            "",
            f"Basis: accepted {field.replace('_', ' ')} transitions to {target}.",
        ]
    if _scope_mode(result) == "projects":
        selected = len((result.get("effective_scope") or {}).get("project_ids") or [])
        lines.append(
            f"\u7bc4\u570d\uff1a\u5df2\u9078\u53d6\u7684 {selected} \u500b\u5c08\u6848\u3002"
            if language == "zh-Hant"
            else f"Scope: {selected} selected projects."
        )
    as_of = _parse_datetime(result.get("as_of"))
    if as_of is not None:
        display = _human_datetime(as_of, language, include_zone=True)
        lines.append(
            f"\u8cc7\u6599\u622a\u81f3\uff1a{display}\u3002"
            if language == "zh-Hant"
            else f"Data as of: {display}."
        )
    return "\n".join(lines)


def _render_telegram(result: dict[str, Any], *, language: str) -> str:
    """Render concise operator text while evidence remains in structured sources."""
    operation = str(result.get("operation") or "")
    filters = result.get("effective_filters") or {}
    raw_transition = filters.get("transition")
    transition = raw_transition if isinstance(raw_transition, dict) else None

    if operation == "count":
        lines = [_telegram_count_heading(result, int(result.get("value") or 0), language)]
    elif operation == "exists":
        exists = bool(result.get("value"))
        if language == "zh-Hant":
            lines = ["\u2705 \u6709\u7b26\u5408\u689d\u4ef6\u7684\u5c08\u6848\u3002" if exists else "\u6c92\u6709\u7b26\u5408\u689d\u4ef6\u7684\u5c08\u6848\u3002"]
        else:
            lines = ["A matching project exists." if exists else "No matching project was found."]
    elif operation == "group":
        lines = ["\u5c08\u6848\u5206\u7d44\u7d50\u679c\uff1a" if language == "zh-Hant" else "Project breakdown:"]
        for item in result.get("groups") or []:
            key = _display_value(str(result.get("group_by") or ""), item.get("key"), language)
            delimiter = "\uff1a" if language == "zh-Hant" else ": "
            lines.append(f"\u2022 {key}{delimiter}**{int(item.get('count') or 0)}**")
    else:
        total = int(result.get("total") or 0)
        lines = [_telegram_list_heading(result, total, language)]
        for row in result.get("results") or []:
            lines.append("")
            lines.extend(
                _telegram_row_lines(
                    row,
                    fields=list(result.get("fields") or []),
                    transition=transition,
                    language=language,
                )
            )
        if result.get("next_cursor"):
            lines.extend([
                "",
                "\u4e0b\u4e00\u9801\u4ecd\u6709\u66f4\u591a\u7b26\u5408\u689d\u4ef6\u7684\u5c08\u6848\u3002"
                if language == "zh-Hant"
                else "More matching projects are available on the next page.",
            ])

    footer = _telegram_footer(result, transition=transition, language=language)
    if footer:
        lines.append("")
        lines.extend(footer)
    return "\n".join(lines)


def _telegram_count_heading(result: dict[str, Any], count: int, language: str) -> str:
    filters = result.get("effective_filters") or {}
    transition = filters.get("transition")
    if isinstance(transition, dict):
        target = _joined_values(str(transition.get("field") or ""), transition.get("to_values") or [], language)
        period = _period_description(result, transition, language)
        if language == "zh-Hant":
            return f"{period}\u5171\u6709 **{count}** \u500b\u5c08\u6848\u8f49\u70ba\u300c{target}\u300d\u3002"
        return f"**{count}** projects changed to {target} {period}."
    if language == "zh-Hant":
        label = "\u5c08\u6848\u7e3d\u6578" if not _has_filters(filters) and _scope_mode(result) == "portfolio" else "\u7b26\u5408\u689d\u4ef6\u7684\u5c08\u6848"
        return f"{label}\uff1a**{count}** \u500b"
    label = "Total projects" if not _has_filters(filters) and _scope_mode(result) == "portfolio" else "Matching projects"
    return f"{label}: **{count}**"


def _telegram_list_heading(result: dict[str, Any], total: int, language: str) -> str:
    filters = result.get("effective_filters") or {}
    transition = filters.get("transition")
    if isinstance(transition, dict):
        target = _joined_values(str(transition.get("field") or ""), transition.get("to_values") or [], language)
        period = _period_description(result, transition, language)
        if language == "zh-Hant":
            prefix = "\u2705 " if total else ""
            return f"{prefix}{period}\u5171\u6709 **{total}** \u500b\u5c08\u6848\u8f49\u70ba\u300c{target}\u300d\uff1a"
        prefix = "\u2705 " if total else ""
        return f"{prefix}**{total}** projects changed to {target} {period}:"
    return (
        f"\u5171\u627e\u5230 **{total}** \u500b\u5c08\u6848\uff1a"
        if language == "zh-Hant"
        else f"**{total}** projects found:"
    )


def _telegram_row_lines(
    row: dict[str, Any],
    *,
    fields: list[str],
    transition: dict[str, Any] | None,
    language: str,
) -> list[str]:
    project = str(row.get("project_id") or ("\u5c08\u6848" if language == "zh-Hant" else "Project"))
    client = str(row.get("client_name") or "").strip()
    first = f"\u2022 **{project}**"
    if client:
        first += f" \u2014 {client}"
    lines = [first]
    transition_field = str((transition or {}).get("field") or "")
    targets = list((transition or {}).get("to_values") or [])

    for field in fields:
        if field in {"project_id", "client_name"} or (transition and field == transition_field):
            continue
        value = row.get(field)
        if field == "transitioned_at" and transition:
            target = _joined_values(transition_field, targets, language)
            label = _transition_label(target, language)
            display = _human_date(value, language, include_year=False)
        else:
            label = _telegram_field_label(field, language=language)
            display = _display_value(field, value, language)
        delimiter = "\uff1a" if language == "zh-Hant" else ": "
        lines.append(f"  {label}{delimiter}{display}")
    return lines


def _telegram_footer(
    result: dict[str, Any],
    *,
    transition: dict[str, Any] | None,
    language: str,
) -> list[str]:
    lines: list[str] = []
    if transition:
        date_range = _human_date_range(
            transition.get("effective_from"),
            transition.get("effective_to"),
            language,
        )
        if date_range:
            lines.append(
                f"\u67e5\u8a62\u671f\u9593\uff1a{date_range}"
                if language == "zh-Hant"
                else f"Period: {date_range}"
            )
    else:
        summary = _telegram_filter_summary(result.get("effective_filters") or {}, language)
        if summary:
            lines.append(
                f"\u7be9\u9078\uff1a{summary}"
                if language == "zh-Hant"
                else f"Filters: {summary}"
            )

    if _scope_mode(result) == "projects":
        count = len((result.get("effective_scope") or {}).get("project_ids") or [])
        lines.append(
            f"\u7bc4\u570d\uff1a\u5df2\u9078\u7684 {count} \u500b\u5c08\u6848"
            if language == "zh-Hant"
            else f"Scope: {count} selected projects"
        )

    as_of = _parse_datetime(result.get("as_of"))
    if as_of is not None:
        display = _human_datetime(as_of, language, include_zone=True)
        lines.append(
            f"\u8cc7\u6599\u622a\u81f3\uff1a{display}"
            if language == "zh-Hant"
            else f"Data as of: {display}"
        )
    return lines


def _telegram_filter_summary(filters: dict[str, Any], language: str) -> str:
    parts: list[str] = []
    for field in ("project_id", "project_status", "current_milestone", "ball_with", "market"):
        values = filters.get(field) or []
        if values:
            label = _telegram_field_label(field, language=language)
            display = _joined_values(field, values, language)
            parts.append(f"{label}\u70ba\u300c{display}\u300d" if language == "zh-Hant" else f"{label} is {display}")
    client = str(filters.get("client_name") or "").strip()
    if client:
        parts.append(f"\u5ba2\u6236\u540d\u7a31\u5305\u542b\u300c{client}\u300d" if language == "zh-Hant" else f"client name contains {client}")
    date_range = _human_date_range(filters.get("target_go_live_from"), filters.get("target_go_live_to"), language)
    if date_range:
        parts.append(f"\u76ee\u6a19\u4e0a\u7dda\u65e5\u671f\u70ba{date_range}" if language == "zh-Hant" else f"target go-live is {date_range}")
    return ("\uff1b" if language == "zh-Hant" else "; ").join(parts)


def _period_description(result: dict[str, Any], transition: dict[str, Any], language: str) -> str:
    start = _parse_date(transition.get("effective_from"))
    end = _parse_date(transition.get("effective_to"))
    as_of = _parse_datetime(result.get("as_of"))
    if start and end:
        if start == date(end.year, 1, 1):
            if language == "zh-Hant":
                return f"{end.year} \u5e74\u622a\u81f3 {end.month} \u6708 {end.day} \u65e5\uff0c"
            return f"in {end.year} through {end.strftime('%b')} {end.day}"
        days = (end - start).days + 1
        recent = as_of is not None and end == as_of.astimezone(_HONG_KONG).date()
        if language == "zh-Hant":
            return f"\u6700\u8fd1 {days} \u65e5" if recent else f"\u9019 {days} \u65e5\u5167"
        return f"in the last {days} days" if recent else f"during this {days}-day period"
    return "\u6307\u5b9a\u671f\u9593\u5167" if language == "zh-Hant" else "during the requested period"


def _transition_label(target: str, language: str) -> str:
    if language == "zh-Hant":
        return "\u8f49\u70ba\u5b8c\u6210" if target == "\u5df2\u5b8c\u6210" else f"\u8f49\u70ba{target}"
    return f"Changed to {target}"


def _display_value(field: str, value: Any, language: str) -> str:
    if value in (None, ""):
        return "\u672a\u8a2d\u5b9a" if language == "zh-Hant" else "Not set"
    if field == "project_status" and language == "zh-Hant":
        return _STATUS_LABELS_ZH.get(str(value), str(value))
    if field == "ball_with" and language == "zh-Hant":
        return _BALL_WITH_LABELS_ZH.get(str(value), str(value))
    if field in {"target_go_live", "transitioned_at"}:
        return _human_date(value, language, include_year=True)
    if field == "updated_at":
        parsed = _parse_datetime(value)
        return _human_datetime(parsed, language, include_zone=False) if parsed else str(value)
    return str(value)


def _joined_values(field: str, values: list[Any], language: str) -> str:
    delimiter = "\u3001" if language == "zh-Hant" else ", "
    return delimiter.join(_display_value(field, value, language) for value in values)


def _has_filters(filters: dict[str, Any]) -> bool:
    return any(bool(filters.get(key)) for key in (
        "project_id", "project_status", "current_milestone", "ball_with", "market",
        "client_name", "target_go_live_from", "target_go_live_to", "transition",
    ))


def _scope_mode(result: dict[str, Any]) -> str:
    return str((result.get("effective_scope") or {}).get("mode") or "portfolio")


def _parse_datetime(value: Any) -> datetime | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _parse_date(value: Any) -> date | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    try:
        return date.fromisoformat(normalized[:10])
    except ValueError:
        parsed = _parse_datetime(normalized)
        return parsed.date() if parsed else None


def _human_date(value: Any, language: str, *, include_year: bool) -> str:
    parsed_datetime = _parse_datetime(value)
    parsed = parsed_datetime.astimezone(_HONG_KONG).date() if parsed_datetime else _parse_date(value)
    if parsed is None:
        return str(value or ("\u672a\u8a2d\u5b9a" if language == "zh-Hant" else "Not set"))
    if language == "zh-Hant":
        return (
            f"{parsed.year}\u5e74{parsed.month}\u6708{parsed.day}\u65e5"
            if include_year
            else f"{parsed.month}\u6708{parsed.day}\u65e5"
        )
    month_day = f"{parsed.strftime('%b')} {parsed.day}"
    return f"{month_day}, {parsed.year}" if include_year else month_day


def _human_datetime(value: datetime, language: str, *, include_zone: bool) -> str:
    local = value.astimezone(_HONG_KONG)
    if language == "zh-Hant":
        suffix = "\uff08\u9999\u6e2f\u6642\u9593\uff09" if include_zone else ""
        return f"{local.year}\u5e74{local.month}\u6708{local.day}\u65e5 {local:%H:%M}{suffix}"
    suffix = " HKT" if include_zone else ""
    return f"{local.strftime('%b')} {local.day}, {local.year} at {local:%H:%M}{suffix}"


def _human_date_range(start_value: Any, end_value: Any, language: str) -> str:
    start = _parse_date(start_value)
    end = _parse_date(end_value)
    if start and end:
        if language == "zh-Hant":
            if start.year == end.year:
                return f"{start.year}\u5e74{start.month}\u6708{start.day}\u65e5\u81f3{end.month}\u6708{end.day}\u65e5"
            return f"{start.year}\u5e74{start.month}\u6708{start.day}\u65e5\u81f3{end.year}\u5e74{end.month}\u6708{end.day}\u65e5"
        if start.year == end.year:
            return f"{start.strftime('%b')} {start.day}-{end.strftime('%b')} {end.day}, {end.year}"
        return f"{start.strftime('%b')} {start.day}, {start.year}-{end.strftime('%b')} {end.day}, {end.year}"
    if start:
        return f"{start.year}\u5e74{start.month}\u6708{start.day}\u65e5\u8d77" if language == "zh-Hant" else f"from {start.strftime('%b')} {start.day}, {start.year}"
    if end:
        return f"\u622a\u81f3{end.year}\u5e74{end.month}\u6708{end.day}\u65e5" if language == "zh-Hant" else f"through {end.strftime('%b')} {end.day}, {end.year}"
    return ""


def _render_scope(result: dict[str, Any], *, language: str) -> str:
    scope = result.get("effective_scope") or {}
    if scope.get("mode") == "projects":
        count = len(scope.get("project_ids") or [])
        return f"\u5df2\u9078\u5c08\u6848\u7bc4\u570d\uff1a{count} \u500b\u5c08\u6848" if language == "zh-Hant" else f"selected project scope: {count} projects"
    return "\u6574\u9ad4\u5c08\u6848\u7bc4\u570d" if language == "zh-Hant" else "portfolio scope"


def _render_filters(filters: dict[str, Any], *, language: str) -> str:
    labels: list[str] = []
    for key in ("project_id", "project_status", "current_milestone", "ball_with", "market"):
        values = filters.get(key) or []
        if values:
            labels.append(f"{_field_label(key, language=language)}={','.join(str(item) for item in values)}")
    if filters.get("client_name"):
        relation = "\u5305\u542b" if language == "zh-Hant" else "contains"
        labels.append(f"{_field_label('client_name', language=language)} {relation} {filters['client_name']}")
    if filters.get("target_go_live_from"):
        labels.append(f"{_field_label('target_go_live', language=language)} >= {filters['target_go_live_from']}")
    if filters.get("target_go_live_to"):
        labels.append(f"{_field_label('target_go_live', language=language)} <= {filters['target_go_live_to']}")
    transition = filters.get("transition")
    if isinstance(transition, dict):
        field = _field_label(str(transition.get("field") or ""), language=language)
        values = ",".join(str(item) for item in transition.get("to_values") or [])
        date_range = _render_date_range(str(transition.get("effective_from") or ""), str(transition.get("effective_to") or ""), language=language)
        labels.append(f"{field}\u65bc{date_range}\u8f49\u70ba {values}" if language == "zh-Hant" else f"{field} changed to {values} {date_range}")
    if not labels:
        return "\u6c92\u6709\u984d\u5916\u7be9\u9078" if language == "zh-Hant" else "no additional filters"
    return ("\u3001" if language == "zh-Hant" else ", ").join(labels)


def _render_date_range(start: str, end: str, *, language: str) -> str:
    if language == "zh-Hant":
        if start and end:
            return f"{start} \u81f3 {end}"
        if start:
            return f"{start} \u8d77"
        if end:
            return f"{end} \u6216\u4e4b\u524d"
        return "\u6307\u5b9a\u671f\u9593"
    if start and end:
        return f"from {start} to {end}"
    if start:
        return f"on or after {start}"
    if end:
        return f"on or before {end}"
    return "during the requested period"


def _telegram_field_label(field: str, *, language: str) -> str:
    if language != "zh-Hant":
        return {
            "project_id": "Project ID", "client_name": "Client", "project_status": "Status",
            "current_milestone": "Current milestone", "ball_with": "Owner", "market": "Market",
            "next_action": "Next action", "target_go_live": "Target go-live",
            "updated_at": "Last updated",
            "transitioned_at": "Status changed",
        }.get(field, field)
    return _field_label(field, language=language)


def _field_label(field: str, *, language: str) -> str:
    if language != "zh-Hant":
        return {"next_action": "Next action"}.get(field, field)
    return {
        "project_id": "\u5c08\u6848\u7de8\u865f", "client_name": "\u5ba2\u6236",
        "project_status": "\u5c08\u6848\u72c0\u614b", "current_milestone": "\u76ee\u524d\u91cc\u7a0b\u7891",
        "ball_with": "\u8ddf\u9032\u65b9", "market": "\u5e02\u5834",
        "next_action": "\u4e0b\u4e00\u6b65", "target_go_live": "\u76ee\u6a19\u4e0a\u7dda\u65e5\u671f",
        "updated_at": "\u6700\u5f8c\u66f4\u65b0",
        "transitioned_at": "\u72c0\u614b\u8f49\u63db\u6642\u9593",
    }.get(field, field)


__all__ = ["render_portfolio_query_v2"]
