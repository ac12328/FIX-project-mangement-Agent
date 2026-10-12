"""Rendering and atomic writes for grounding-repair oracle-ceiling reports."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.assistant.evals.artifacts import content_hash


def render_oracle_ceiling_markdown(artifact: dict[str, Any]) -> str:
    scope = artifact["scope"]
    strict = artifact["strict_review_oracle"]
    noninferior = artifact["noninferiority_review_oracle"]
    lines = [
        "# Grounding Repair Policy v3 Retrospective Oracle Ceiling",
        "",
        f"- Cohort: `{artifact['cohort_id']}`",
        f"- Source policy: `{artifact['source_policy_version']}`",
        f"- Analysis: `{artifact['analysis_version']}`",
        f"- Generated: `{artifact['generated_at']}`",
        "- Decision: **NOT READY - SHADOW ONLY**",
        "- Payload policy: aggregate metadata only; no questions, evidence, or answers persisted.",
        "",
        "## Scope",
        "",
        f"- Qualifying observations: {scope['qualifying_observation_count']}",
        f"- Repair cases / controls: {scope['repair_case_count']} / {scope['no_repair_control_count']}",
        f"- Completed repair reviews: {scope['completed_review_count']}",
        f"- Organic real-user cases: {scope['organic_real_user_case_count']}",
        f"- Source-policy projected skips: {scope['source_policy_projected_skip_case_count']}",
        "",
        "## Strict review oracle",
        "",
        "Definition: Candidate passed its completed review and had zero Candidate issue codes.",
        "",
        f"- Eligible upper-bound cases: {strict['eligible_case_count']} / {scope['repair_case_count']}",
        f"- Theoretical repair reduction ceiling: {strict['theoretical_repair_call_reduction_ceiling']:.2%}",
        f"- Cluster counts: `{json.dumps(strict['cluster_counts'], sort_keys=True)}`",
        f"- Candidate issue count: {strict['candidate_issue_count']}",
        f"- Mean Candidate-minus-Baseline score: {strict['paired_candidate_minus_baseline_score']['mean']}",
        f"- Median theoretical completion saving: {strict['theoretical_completion_savings_ms']['p50']} ms",
        "",
        "## Three-point noninferiority review oracle",
        "",
        "Definition: Candidate score was within three points of Baseline and had zero Candidate issue codes.",
        "",
        f"- Eligible upper-bound cases: {noninferior['eligible_case_count']} / {scope['repair_case_count']}",
        f"- Theoretical repair reduction ceiling: {noninferior['theoretical_repair_call_reduction_ceiling']:.2%}",
        f"- Cluster counts: `{json.dumps(noninferior['cluster_counts'], sort_keys=True)}`",
        f"- Candidate issue count: {noninferior['candidate_issue_count']}",
        f"- Median theoretical completion saving: {noninferior['theoretical_completion_savings_ms']['p50']} ms",
        "",
        "## Interpretation boundary",
        "",
        "This is a retrospective outcome oracle, not a runtime selector. It uses completed review "
        "labels that do not exist before serving, so it is only an upper bound. No policy, context, "
        "loop, runtime, serving, or promotion change is authorized.",
        "",
        "## Next offline experiment gates",
        "",
    ]
    for key, value in artifact["predeclared_next_experiment_gates"].items():
        lines.append(f"- `{key}`: `{value}`")
    lines.extend([
        "",
        "Controlled simulation must not be relabeled as organic evidence.",
        "",
        f"Content hash: `{artifact['content_hash']}`",
    ])
    return "\n".join(lines).rstrip() + "\n"


def write_oracle_ceiling_report(
    artifact: dict[str, Any],
    json_path: Path,
    markdown_path: Path | None = None,
) -> tuple[Path, Path]:
    if artifact.get("content_hash") != content_hash(artifact):
        raise ValueError("oracle-ceiling artifact content hash is invalid")
    json_destination = Path(json_path).resolve()
    markdown_destination = (
        Path(markdown_path).resolve()
        if markdown_path is not None
        else json_destination.with_suffix(".md")
    )
    json_destination.parent.mkdir(parents=True, exist_ok=True)
    markdown_destination.parent.mkdir(parents=True, exist_ok=True)
    json_temp = json_destination.with_suffix(json_destination.suffix + ".tmp")
    markdown_temp = markdown_destination.with_suffix(markdown_destination.suffix + ".tmp")
    json_temp.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    markdown_temp.write_text(
        render_oracle_ceiling_markdown(artifact),
        encoding="utf-8",
        newline="\n",
    )
    json_temp.replace(json_destination)
    markdown_temp.replace(markdown_destination)
    return json_destination, markdown_destination


__all__ = ["render_oracle_ceiling_markdown", "write_oracle_ceiling_report"]
