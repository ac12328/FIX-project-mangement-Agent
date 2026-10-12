"""Production-equivalent answer preparation for Policy-v4 offline replay."""

from __future__ import annotations

import re
from typing import Any

from src.assistant import grounding
from src.assistant.evals.grounding_repair_v4_replay_cache import (
    ReplayCache,
    _AnswerIndex,
    _attach_project_line_citations,
    _citation_plan,
    _material_email_lines,
)

_MULTISPACE_RE = re.compile(r"[ \t]{2,}")
_UNANSWERED_SCOPE_RE = re.compile(
    r"non[- ]terminal|open projects?|active projects?|include(?:s|d|ing)? terminal|terminal projects?|"
    r"\u975e\s*terminal|\u958b\u555f\u9805\u76ee|\u9032\u884c\u4e2d\u9805\u76ee|"
    r"\u6d3b\u8e8d\u9805\u76ee",
    re.IGNORECASE,
)


def _sanitize_internal_artifacts(answer: str) -> str:
    cleaned = (
        grounding._INTERNAL_TOOL_ARTIFACT_RE.sub("", answer)
        if "monads0x" in answer.casefold()
        else answer
    )
    lines: list[str] = []
    for line in cleaned.splitlines():
        if "  " in line or "\t" in line:
            line = _MULTISPACE_RE.sub(" ", line)
        lines.append(line.rstrip())
    return "\n".join(lines).strip()


def _verified_partial_answer(answer: str, known_sources: frozenset[str]) -> str:
    kept: list[str] = []
    for line in answer.splitlines():
        if grounding._INTERNAL_TOOL_ARTIFACT_RE.search(line):
            continue
        if "[[source:" in line:
            citations = set(grounding._CITATION_RE.findall(line))
            if citations and citations.issubset(known_sources):
                kept.append(line)
            continue
        if line.strip() and not grounding._PROJECT_FACT_RE.search(line):
            kept.append(line)
    warning = "\n\n> Some statements were omitted because the Workspace could not verify their source citations."
    body = "\n".join(kept).strip()
    return (body or "I could not produce a fully verified answer from the available records.") + warning


def _validate_answer(
    answer: str,
    known_sources: frozenset[str],
    factual_tools_used: bool,
) -> list[str]:
    cited: set[str] = set()
    uncited_factual_lines: list[str] = []
    has_internal_marker = False
    scan_internal_marker = "monads0x" in answer.casefold()
    for raw_line in answer.splitlines():
        if scan_internal_marker and grounding._INTERNAL_TOOL_ARTIFACT_RE.search(raw_line):
            has_internal_marker = True
        citations = (
            grounding._CITATION_RE.findall(raw_line)
            if "[[source:" in raw_line
            else ()
        )
        cited.update(citations)
        line = raw_line.strip()
        if not line or line.startswith(("#", "|---", "```")):
            continue
        is_project_fact = bool(grounding._PROJECT_FACT_RE.search(line))
        if (
            is_project_fact
            and not citations
            and not grounding._RECOMMENDATION_RE.search(line)
        ):
            uncited_factual_lines.append(line)

    errors: list[str] = []
    if has_internal_marker:
        errors.append("The answer exposes an internal tool protocol marker")
    unknown = sorted(cited - known_sources)
    if unknown:
        errors.append(f"Unknown citations: {', '.join(unknown)}")
    if factual_tools_used and known_sources and not cited:
        errors.append("The answer contains no source citations")
    for line in uncited_factual_lines:
        errors.append(f"Uncited factual line: {line[:140]}")
        if len(errors) >= 8:
            break
    return errors

def _validate_and_verified_partial_answer(
    answer: str,
    known_sources: frozenset[str],
    factual_tools_used: bool,
) -> tuple[list[str], str]:
    """Analyze the prepared answer once for validation and verified-partial replay."""
    cited: set[str] = set()
    kept: list[str] = []
    uncited_factual_lines: list[str] = []
    has_internal_marker = False
    scan_internal_marker = "monads0x" in answer.casefold()

    for raw_line in answer.splitlines():
        line_has_internal_marker = bool(
            scan_internal_marker and grounding._INTERNAL_TOOL_ARTIFACT_RE.search(raw_line)
        )
        has_internal_marker = has_internal_marker or line_has_internal_marker
        citations = (
            grounding._CITATION_RE.findall(raw_line)
            if "[[source:" in raw_line
            else ()
        )
        cited.update(citations)
        stripped = raw_line.strip()
        is_project_fact = bool(
            stripped and grounding._PROJECT_FACT_RE.search(stripped)
        )

        if not line_has_internal_marker:
            if citations:
                if all(source_id in known_sources for source_id in citations):
                    kept.append(raw_line)
            elif stripped and not is_project_fact:
                kept.append(raw_line)

        if not stripped or stripped.startswith(("#", "|---", "```")):
            continue
        if (
            is_project_fact
            and not citations
            and not grounding._RECOMMENDATION_RE.search(stripped)
        ):
            uncited_factual_lines.append(stripped)

    errors: list[str] = []
    if has_internal_marker:
        errors.append("The answer exposes an internal tool protocol marker")
    unknown = sorted(cited - known_sources)
    if unknown:
        errors.append(f"Unknown citations: {', '.join(unknown)}")
    if factual_tools_used and known_sources and not cited:
        errors.append("The answer contains no source citations")
    for line in uncited_factual_lines:
        errors.append(f"Uncited factual line: {line[:140]}")
        if len(errors) >= 8:
            break

    warning = (
        "\n\n> Some statements were omitted because the Workspace could not "
        "verify their source citations."
    )
    body = "\n".join(kept).strip()
    partial = (
        body or "I could not produce a fully verified answer from the available records."
    ) + warning
    return errors, partial

def _missing_snapshot_summary(
    index: _AnswerIndex,
    tool: str,
    result: dict[str, Any],
    *,
    arguments: dict[str, Any] | None = None,
) -> str:
    source_id = str(result.get("summary_source_id") or "").strip()
    as_of = str(result.get("as_of") or "").strip()
    if not source_id:
        return ""
    arguments = arguments or {}
    summary_complete = source_id in index.cited and (not as_of or as_of in index.text)
    unanswered_filter_detail = ""
    if tool == "list_unanswered_threads":
        filters = result.get("filters") if isinstance(result.get("filters"), dict) else {}
        min_age_hours = filters.get("min_age_hours")
        if min_age_hours is None:
            min_age_hours = arguments.get("min_age_hours")
        include_terminal = bool(arguments.get("include_terminal", False))
        scope_label = (
            "all visible projects, including terminal projects"
            if include_terminal
            else "non-terminal/open projects"
        )
        if isinstance(min_age_hours, (int, float)):
            rendered_age = f"{min_age_hours:g}"
            unanswered_filter_detail = (
                f", using a minimum age of {rendered_age} hours across {scope_label}"
            )
            age_is_present = bool(re.search(
                rf"(?<![0-9]){re.escape(rendered_age)}(?:\.0)?\s*(?:hours?|hrs?|h|\u5c0f\u6642)",
                index.text,
                re.IGNORECASE,
            ))
            summary_complete = (
                summary_complete
                and age_is_present
                and bool(_UNANSWERED_SCOPE_RE.search(index.text))
            )
    if summary_complete:
        return ""
    total = result.get("total")
    if total is None:
        total = result.get("attention_count")
    labels = {
        "portfolio_attention": "portfolio attention scan",
        "search_project_parties": "organisation search",
        "query_portfolio": "portfolio query",
        "get_project_brief": "project brief",
        "search_project_timeline": "project timeline query",
        "list_project_work": "project work query",
        "list_unanswered_threads": "unanswered-thread scan",
    }
    label = labels.get(tool, tool.replace("_", " "))
    if total == 0:
        detail = f"The {label} found no matching records in the active Workspace scope"
    elif total is not None:
        detail = f"The {label} matched {total} records in the active Workspace scope"
    else:
        detail = f"The {label} was evaluated from the active Workspace scope"
    detail += unanswered_filter_detail
    if as_of:
        detail += f" as of {as_of}"
    return f"- **Workspace snapshot:** {detail}. [[source:{source_id}]]"


def _all_present_values_are_mentioned(context: str, values: tuple[Any, ...]) -> bool:
    normalized_context = " ".join(context.split()).casefold()
    return all(
        " ".join(str(value).split()).casefold() in normalized_context
        for value in values
        if value not in (None, "")
    )


def _missing_project_rows(index: _AnswerIndex, tool: str, result: dict[str, Any]) -> str:
    rows = result.get("results") if isinstance(result.get("results"), list) else []
    missing: list[dict[str, Any]] = []
    seen_projects: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        project_id = str(row.get("project_id") or "").strip()
        source_id = str(row.get("source_id") or "").strip()
        if not project_id or not source_id or project_id in seen_projects:
            continue
        seen_projects.add(project_id)
        row_complete = project_id.lower() in index.lower and source_id in index.cited
        if row_complete and tool in {"query_portfolio", "portfolio_attention"}:
            if tool == "query_portfolio":
                values = (
                    row.get("client_name"),
                    row.get("project_status"),
                    row.get("current_milestone"),
                    row.get("ball_with"),
                )
            else:
                values = (
                    row.get("project_status"),
                    row.get("current_milestone"),
                    row.get("ball_with"),
                    row.get("priority_score"),
                    *(row.get("attention_reasons") or []),
                )
            source_context = "\n".join(index.source_lines.get(source_id, ()))
            if source_context and _all_present_values_are_mentioned(source_context, values):
                row_complete = True
            else:
                record_text = project_id.casefold()
                source_marker = f"[[source:{source_id}]]"
                context = "\n".join(
                    line
                    for line, folded in zip(index.lines, index.folded_lines, strict=True)
                    if record_text in folded or source_marker in line
                )
                row_complete = _all_present_values_are_mentioned(context, values)
        if not row_complete:
            missing.append(row)
    if not missing:
        return ""

    titles = {
        "portfolio_attention": "Verified attention records",
        "search_project_parties": "Verified organisation matches",
        "query_portfolio": "Verified portfolio records",
    }
    lines = [f"### {titles[tool]}"]
    for row in missing:
        source_id = row["source_id"]
        project_id = row["project_id"]
        if tool == "search_project_parties":
            detail = (
                f"{row.get('org_name') or 'The organisation'} is recorded as "
                f"{row.get('party_role') or 'a project party'}; status {row.get('project_status') or 'not set'}; "
                f"milestone {row.get('current_milestone') or 'not set'}"
            )
        elif tool == "portfolio_attention":
            reasons = ", ".join(str(value) for value in (row.get("attention_reasons") or [])) or "attention signal"
            detail = (
                f"status {row.get('project_status') or 'not set'}; milestone "
                f"{row.get('current_milestone') or 'not set'}; ball with {row.get('ball_with') or 'not set'}; "
                f"priority {row.get('priority_score')}; {reasons}"
            )
        else:
            detail = (
                f"{row.get('client_name') or 'Client not set'}; status {row.get('project_status') or 'not set'}; "
                f"milestone {row.get('current_milestone') or 'not set'}; "
                f"ball with {row.get('ball_with') or 'not set'}"
            )
        lines.append(f"- **{project_id}** ? {detail}. [[source:{source_id}]]")
    return "\n".join(lines)


def _missing_brief_facts(index: _AnswerIndex, result: dict[str, Any], cache: ReplayCache) -> str:
    project = result.get("project") if isinstance(result.get("project"), dict) else {}
    project_id = str(project.get("project_id") or "").strip()
    project_source = str(project.get("source_id") or "").strip()

    lines: list[str] = []
    current_values = [
        str(project.get("project_status") or ""),
        str(project.get("current_milestone") or ""),
        str(project.get("ball_with") or ""),
    ]
    if project_id and project_source and (
        project_id.lower() not in index.lower
        or any(value and value.lower() not in index.lower for value in current_values)
        or project_source not in index.cited
    ):
        lines.append(
            f"- **Current state:** {project_id} is {project.get('project_status') or 'not set'} at "
            f"{project.get('current_milestone') or 'milestone not set'}, with the ball "
            f"{project.get('ball_with') or 'not set'}. [[source:{project_source}]]"
        )

    next_action = str(project.get("next_action") or "").strip()
    next_terms = [term for term in re.findall(r"[a-z0-9]+", next_action.lower()) if len(term) >= 5]
    next_overlap = {term for term in next_terms if term in index.lower}
    if project_source and next_action and "next action" not in index.lower and len(next_overlap) < 2:
        lines.append(f"- **Next action:** {next_action}. [[source:{project_source}]]")

    emails = result.get("recent_emails") if isinstance(result.get("recent_emails"), list) else []
    activities = result.get("recent_activity") if isinstance(result.get("recent_activity"), list) else []
    material = _material_email_lines(index, emails, cache)
    if material:
        lines.extend(material)

    latest_activity = grounding._latest_record(activities, "created_at")
    if latest_activity:
        source_id = str(latest_activity.get("source_id") or "")
        summary = grounding._single_line(str(latest_activity.get("summary") or ""), limit=280)
        line_citations = set(grounding._CITATION_RE.findall("\n".join(lines)))
        if source_id and source_id not in index.cited | line_citations and summary:
            lines.append(
                f"- **Latest recorded activity ({grounding._record_date(latest_activity, 'created_at')}):** "
                f"{summary}. [[source:{source_id}]]"
            )

    if not lines:
        return ""
    return "### Verified project brief\n" + "\n".join(lines)


def _ensure_tool_completeness(
    answer: str,
    tool_results: list[dict[str, Any]],
    cache: ReplayCache,
    citation_plan: tuple[tuple[str, str, str, re.Pattern[str] | None], ...],
) -> tuple[_AnswerIndex, bool]:
    current = _AnswerIndex(answer)
    summarized_tools: set[str] = set()
    needs_final_citation_pass = False
    for item in tool_results:
        tool = str(item.get("tool") or "")
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        if result.get("error"):
            continue
        if tool not in summarized_tools:
            summary = _missing_snapshot_summary(
                current,
                tool,
                result,
                arguments=item.get("arguments") if isinstance(item.get("arguments"), dict) else {},
            )
            summarized_tools.add(tool)
            if summary:
                needs_final_citation_pass = True
                current.append_section(summary)
        if tool in {"portfolio_attention", "search_project_parties", "query_portfolio"}:
            section = _missing_project_rows(current, tool, result)
        elif tool == "get_project_brief":
            section = _missing_brief_facts(current, result, cache)
        elif tool == "search_project_timeline":
            section = grounding._missing_timeline_rows(current.citation_text, result)
        elif tool == "list_project_work":
            section = grounding._missing_work_rows(current.citation_text, result)
        elif tool == "list_unanswered_threads":
            section = grounding._missing_unanswered_rows(current.citation_text, result)
        elif tool == "get_project_evidence_audit":
            section = grounding._missing_evidence_audit(current.text, result)
        elif tool == "assess_milestone_readiness":
            section = grounding._missing_readiness_assessment(current.text, result)
        else:
            section = ""
        if section:
            needs_final_citation_pass = True
            current.append_section(section)
    return current, needs_final_citation_pass


def _remove_superseded_next_action_lines(
    index: _AnswerIndex,
    tool_results: list[dict[str, Any]],
) -> str:
    """Offline equivalent using citations already indexed during preparation."""
    stale_source_ids, current_source_ids = grounding._superseded_next_action_sources(
        tool_results
    )
    if not stale_source_ids:
        return index.text

    kept: list[str] = []
    for line, citations in zip(index.lines, index.line_citations):
        cites_stale_action = bool(citations & stale_source_ids)
        cites_current_truth = bool(citations & current_source_ids)
        if (
            cites_stale_action
            and not cites_current_truth
            and grounding._NEXT_ACTION_CLAIM_RE.search(line)
        ):
            continue
        kept.append(line)
    return "\n".join(kept)


def _prepare_answer_indexed(
    answer: str,
    tool_results: list[dict[str, Any]],
    cache: ReplayCache,
    citation_plan: tuple[tuple[str, str, str, re.Pattern[str] | None], ...],
) -> str:
    candidate = _sanitize_internal_artifacts(str(answer or "").strip())
    candidate = grounding._enforce_evidence_audit_conflict_truth(candidate, tool_results)
    candidate = _attach_project_line_citations(candidate, citation_plan)
    current, needs_final_citation_pass = _ensure_tool_completeness(
        candidate, tool_results, cache, citation_plan
    )
    candidate = _remove_superseded_next_action_lines(current, tool_results)
    if needs_final_citation_pass:
        candidate = _attach_project_line_citations(candidate, citation_plan)
    return candidate.strip()


def prepare_answer_for_replay(
    answer: str,
    sources: list[dict[str, Any]],
    tool_results: list[dict[str, Any]],
    cache: ReplayCache,
) -> str:
    """Replay ``grounding._prepare_answer`` with equivalent offline-only acceleration."""
    return _prepare_answer_indexed(
        answer, tool_results, cache, _citation_plan(sources, cache)
    )


__all__ = ["prepare_answer_for_replay"]
