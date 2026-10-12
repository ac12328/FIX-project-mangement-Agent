"""Render complete AI Workspace evaluation results as JSON and Markdown."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .models import CandidateObservation, CaseRunResult, EvalRunResult, JudgeResult

_BACKTICK_RUN_RE = re.compile(r"`+")


def _as_run_result(result: EvalRunResult | dict[str, Any]) -> EvalRunResult:
    if isinstance(result, EvalRunResult):
        return result
    return EvalRunResult.model_validate(result)


def build_json_report(result: EvalRunResult | dict[str, Any]) -> dict[str, Any]:
    """Return the lossless JSON report payload for one eval run."""
    return _as_run_result(result).model_dump(mode="json")


def render_json_report(
    result: EvalRunResult | dict[str, Any],
    *,
    indent: int = 2,
) -> str:
    """Serialize an eval run using stable, human-readable UTF-8 JSON."""
    return json.dumps(
        build_json_report(result),
        ensure_ascii=False,
        indent=indent,
        sort_keys=False,
    ) + "\n"


def _inline_code(value: Any) -> str:
    text = str(value)
    longest = max((len(run) for run in _BACKTICK_RUN_RE.findall(text)), default=0)
    fence = "`" * max(1, longest + 1)
    padding = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{padding}{text}{padding}{fence}"


def _fenced_block(value: Any, language: str = "") -> str:
    text = str(value or "")
    longest = max((len(run) for run in _BACKTICK_RUN_RE.findall(text)), default=0)
    fence = "`" * max(3, longest + 1)
    body = text.rstrip("\n")
    return f"{fence}{language}\n{body}\n{fence}"


def _json_block(value: Any) -> str:
    return _fenced_block(
        json.dumps(value, ensure_ascii=False, indent=2, default=str),
        "json",
    )


def _status(passed: bool) -> str:
    return "PASS" if passed else "FAIL"


def _list_or_none(values: list[Any], *, formatter=str) -> list[str]:
    if not values:
        return ["- None"]
    return [f"- {formatter(value)}" for value in values]


def _source_ids(evidence: dict[str, Any]) -> list[str]:
    seen: set[str] = set()
    source_ids: list[str] = []
    for source in evidence.get("sources") or []:
        if not isinstance(source, dict):
            continue
        source_id = str(source.get("source_id") or "").strip()
        if source_id and source_id not in seen:
            seen.add(source_id)
            source_ids.append(source_id)
    return source_ids


def _candidate_source_ids(candidate: CandidateObservation) -> list[str]:
    seen: set[str] = set()
    source_ids: list[str] = []
    for source in candidate.source_parts:
        source_id = str(source.get("source_id") or "").strip()
        if source_id and source_id not in seen:
            seen.add(source_id)
            source_ids.append(source_id)
    return source_ids


def _case_failure_classes(case: CaseRunResult) -> list[str]:
    return sorted(
        {
            failure.value if hasattr(failure, "value") else str(failure)
            for judge in case.repetitions
            for failure in judge.failure_classes
        }
    )


def _case_recommended_fixes(case: CaseRunResult) -> list[str]:
    return list(
        dict.fromkeys(
            judge.recommended_agent_fix.strip()
            for judge in case.repetitions
            if judge.recommended_agent_fix.strip()
        )
    )


def _render_evidence(case: CaseRunResult) -> list[str]:
    evidence = case.evidence
    lines = ["### Evidence"]
    if not evidence:
        return [*lines, "", "_No evidence payload was recorded._"]

    lines.extend(
        [
            "",
            f"- Tool: {_inline_code(evidence.get('tool_name') or 'unknown')}",
            f"- As of: {_inline_code(evidence.get('as_of') or 'unknown')}",
            f"- Data watermark: {_inline_code(evidence.get('data_watermark') or 'unknown')}",
            f"- Content hash: {_inline_code(evidence.get('content_hash') or 'missing')}",
        ]
    )
    tool_result = evidence.get("tool_result")
    if isinstance(tool_result, dict):
        summary_items = []
        for key in (
            "total",
            "count",
            "returned",
            "pages_fetched",
            "summary_source_id",
            "next_cursor",
        ):
            if key in tool_result:
                summary_items.append(f"{key}={tool_result[key]!r}")
        if summary_items:
            lines.append(f"- Result summary: {', '.join(summary_items)}")

    sources = evidence.get("sources") or []
    lines.extend(["", "#### Evidence sources"])
    if not sources:
        lines.append("- None")
    else:
        for source in sources:
            if not isinstance(source, dict):
                lines.append(f"- {source}")
                continue
            source_id = source.get("source_id") or "unknown"
            descriptors = [
                str(value)
                for value in (
                    source.get("kind"),
                    source.get("title"),
                    source.get("project_id"),
                    source.get("occurred_at"),
                )
                if value
            ]
            suffix = f" — {' | '.join(descriptors)}" if descriptors else ""
            lines.append(f"- {_inline_code(source_id)}{suffix}")
            if source.get("locator"):
                lines.append(f"  - Locator: {_inline_code(source['locator'])}")
            if source.get("snippet"):
                lines.append(f"  - Snippet: {source['snippet']}")

    lines.extend(
        [
            "",
            "<details>",
            "<summary>Evidence tool arguments and result</summary>",
            "",
            _json_block(
                {
                    "tool_args": evidence.get("tool_args") or {},
                    "tool_result": evidence.get("tool_result") or {},
                    "redactions": evidence.get("redactions") or [],
                }
            ),
            "",
            "</details>",
        ]
    )
    return lines


def _render_golden(case: CaseRunResult) -> list[str]:
    golden = case.golden
    lines = ["### Golden specification"]
    if not golden:
        return [*lines, "", "_No golden specification was recorded._"]

    lines.extend(
        [
            "",
            "#### Golden answer",
            "",
            _fenced_block(golden.get("golden_answer") or ""),
            "",
            "#### Required claims",
        ]
    )
    claims = golden.get("required_claims") or []
    if not claims:
        lines.append("- None")
    else:
        for claim in claims:
            if not isinstance(claim, dict):
                lines.append(f"- {claim}")
                continue
            source_ids = ", ".join(
                _inline_code(item)
                for item in claim.get("supporting_source_ids") or []
            ) or "none"
            critical = "critical" if claim.get("critical", True) else "non-critical"
            lines.append(
                f"- {_inline_code(claim.get('claim_id') or 'unknown')} "
                f"({critical}; sources: {source_ids}): "
                f"{claim.get('description') or ''}"
            )

    lines.extend(["", "#### Forbidden claims"])
    forbidden = golden.get("forbidden_claims") or []
    if not forbidden:
        lines.append("- None")
    else:
        for claim in forbidden:
            if not isinstance(claim, dict):
                lines.append(f"- {claim}")
                continue
            critical = "critical" if claim.get("critical", True) else "non-critical"
            lines.append(
                f"- {_inline_code(claim.get('claim_id') or 'unknown')} "
                f"({critical}): {claim.get('description') or ''}"
            )
    if golden.get("judge_notes"):
        lines.extend(["", "#### Judge notes", "", str(golden["judge_notes"])])
    return lines


def _render_candidate(candidate: CandidateObservation | None) -> list[str]:
    if candidate is None:
        return ["#### Candidate observation", "", "_No candidate observation was recorded._"]

    source_ids = _candidate_source_ids(candidate)
    lines = [
        "#### Candidate observation",
        "",
        f"- Elapsed: {candidate.elapsed_seconds:.2f}s",
        f"- Tools: {', '.join(_inline_code(tool) for tool in candidate.actual_tools) or 'none'}",
        f"- Source IDs: {', '.join(_inline_code(item) for item in source_ids) or 'none'}",
        f"- Error: {candidate.error or 'none'}",
        f"- Validation errors: {len(candidate.validation_errors)}",
        "",
        "##### Candidate answer",
        "",
        _fenced_block(candidate.answer),
        "",
        "##### Validation errors",
        *_list_or_none(candidate.validation_errors),
        "",
        "##### Tool trace",
        "",
        _json_block(candidate.tool_trace),
    ]
    if candidate.progress:
        lines.extend(
            [
                "",
                "<details>",
                "<summary>Progress events</summary>",
                "",
                _json_block(candidate.progress),
                "",
                "</details>",
            ]
        )
    return lines


def _render_judge(judge: JudgeResult | None) -> list[str]:
    if judge is None:
        return ["#### Judge result", "", "_No judge result was recorded._"]

    failure_classes = [
        item.value if hasattr(item, "value") else str(item)
        for item in judge.failure_classes
    ]
    lines = [
        "#### Judge result",
        "",
        f"- Result: **{_status(judge.overall_pass)}**",
        f"- Score: **{judge.score_0_to_100}/100**",
        f"- Tool selection: {_status(judge.tool_selection_pass)}",
        f"- Tool arguments/scope: {_status(judge.tool_args_pass)}",
        f"- Grounding/citations: {_status(judge.grounding_pass)}",
        f"- Judge model: {_inline_code(judge.judge_model or 'not recorded')}",
        f"- Judge infrastructure error: {judge.judge_error or 'none'}",
        f"- Confidence: {judge.confidence:.2f}",
        f"- Failure classes: {', '.join(_inline_code(item) for item in failure_classes) or 'none'}",
        "",
        "##### Required claim results",
    ]
    if judge.required_claim_results:
        for claim in judge.required_claim_results:
            lines.append(
                f"- **{_status(claim.passed)}** {_inline_code(claim.claim_id)}: "
                f"{claim.reason}"
            )
    else:
        lines.append("- None")

    sections = (
        ("Judge differences", judge.main_differences),
        ("Forbidden claim violations", judge.forbidden_claim_violations),
        ("Citation issues", judge.citation_issues),
    )
    for heading, values in sections:
        lines.extend(["", f"##### {heading}", *_list_or_none(values)])

    lines.extend(["", "##### Deterministic gates"])
    if judge.deterministic_gates:
        for gate in judge.deterministic_gates:
            blocking = "blocking" if gate.blocking else "advisory"
            failure = (
                gate.failure_class.value
                if gate.failure_class is not None and hasattr(gate.failure_class, "value")
                else gate.failure_class
            )
            suffix = f"; failure={failure}" if failure else ""
            lines.append(
                f"- **{_status(gate.passed)}** {_inline_code(gate.gate_id)} "
                f"({blocking}{suffix}): {gate.reason}"
            )
            if gate.details:
                lines.extend(["", _json_block(gate.details)])
    else:
        lines.append("- None")

    lines.extend(
        [
            "",
            "##### Recommended agent fix",
            "",
            judge.recommended_agent_fix or "_No recommended fix was recorded._",
        ]
    )
    return lines


def _render_repetitions(case: CaseRunResult) -> list[str]:
    lines = ["### Candidate observations and judge results"]
    candidates = {candidate.repetition: candidate for candidate in case.candidates}
    judges = {judge.repetition: judge for judge in case.repetitions}
    repetition_ids = sorted(set(candidates) | set(judges))
    if not repetition_ids:
        return [*lines, "", "_No candidate observations or judge results were recorded._"]

    for repetition in repetition_ids:
        lines.extend(
            [
                "",
                f"#### Repetition {repetition}",
                "",
                *_render_candidate(candidates.get(repetition)),
                "",
                *_render_judge(judges.get(repetition)),
            ]
        )
    return lines


def _render_case(case: CaseRunResult) -> list[str]:
    failure_classes = _case_failure_classes(case)
    recommended_fixes = _case_recommended_fixes(case)
    source_ids = _source_ids(case.evidence)
    lines = [
        f"## Case {_inline_code(case.case_id)} — {_status(case.overall_pass)}",
        "",
        f"- Mean score: **{case.mean_score:.2f}/100**",
        f"- Passes: **{case.pass_count}/{len(case.repetitions)}** "
        f"(minimum {case.minimum_passes})",
        f"- Flaky: {'yes' if case.flaky else 'no'}",
        f"- Evidence source IDs: {', '.join(_inline_code(item) for item in source_ids) or 'none'}",
        f"- Failure classes: {', '.join(_inline_code(item) for item in failure_classes) or 'none'}",
        "",
        "### Question",
        "",
        _fenced_block(case.question),
        "",
        "### PM mining provenance",
        "",
        f"- PM intent: {case.pm_intent or 'not recorded'}",
        f"- PM rationale: {case.pm_rationale or 'not recorded'}",
        f"- Mining batch: {_inline_code(case.mining_batch_id or 'not recorded')}",
        f"- Email signal IDs: {', '.join(_inline_code(item) for item in case.source_signal_ids) or 'none'}",
        f"- Question review: {_inline_code(json.dumps(case.question_review, ensure_ascii=False, sort_keys=True)) if case.question_review else 'not recorded'}",
        "",
        *_render_evidence(case),
        "",
        *_render_golden(case),
        "",
        *_render_repetitions(case),
        "",
        "### Case-level recommended fixes",
        *_list_or_none(recommended_fixes),
    ]
    return lines


def render_markdown_report(result: EvalRunResult | dict[str, Any]) -> str:
    """Render a complete per-case diagnostic report for human review."""
    run = _as_run_result(result)
    duration = max(0.0, (run.completed_at - run.started_at).total_seconds())
    lines = [
        "# AI Workspace Evaluation Report",
        "",
        f"- Run: {_inline_code(run.run_id)}",
        f"- Suite: {_inline_code(run.suite_id)}",
        f"- Mode: {_inline_code(run.mode)}",
        f"- Started: {_inline_code(run.started_at.isoformat())}",
        f"- Completed: {_inline_code(run.completed_at.isoformat())}",
        f"- Duration: {duration:.2f}s",
        f"- Result: **{_status(run.all_passed)}**",
        f"- Cases passed: **{run.passed_cases}/{run.total_cases}**",
        f"- Run content hash: {_inline_code(run.content_hash or 'missing')}",
        "",
        "## Artifact hashes",
        *_list_or_none(
            list(run.artifact_hashes.items()),
            formatter=lambda item: f"{_inline_code(item[0])}: {_inline_code(item[1])}",
        ),
    ]
    for case in run.cases:
        lines.extend(["", "---", "", *_render_case(case)])
    return "\n".join(lines).rstrip() + "\n"


def _write_text(path: Path, content: str) -> Path:
    resolved = path.resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    temporary = resolved.with_suffix(resolved.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(resolved)
    return resolved


def write_eval_report(
    result: EvalRunResult | dict[str, Any],
    json_path: Path,
    markdown_path: Path | None = None,
) -> tuple[Path, Path]:
    """Write paired JSON and Markdown reports and return their resolved paths."""
    json_destination = Path(json_path)
    markdown_destination = (
        Path(markdown_path)
        if markdown_path is not None
        else json_destination.with_suffix(".md")
    )
    written_json = _write_text(json_destination, render_json_report(result))
    written_markdown = _write_text(
        markdown_destination,
        render_markdown_report(result),
    )
    return written_json, written_markdown


def write_report(
    result: EvalRunResult | dict[str, Any],
    json_path: Path,
    markdown_path: Path | None = None,
) -> tuple[Path, Path]:
    """Compatibility alias for ``write_eval_report``."""
    return write_eval_report(result, json_path, markdown_path)


__all__ = [
    "build_json_report",
    "render_json_report",
    "render_markdown_report",
    "write_eval_report",
    "write_report",
]
