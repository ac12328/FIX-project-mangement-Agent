"""Rendering and atomic writes for grounding-repair failure-cluster reports."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.assistant.evals.artifacts import content_hash


def render_failure_cluster_markdown(artifact: dict[str, Any]) -> str:
    scope = artifact["scope"]
    lines = [
        "# Grounding Repair Policy v2 Failure-Cluster Analysis",
        "",
        f"- Cohort: `{artifact['cohort_id']}`",
        f"- Policy: `{artifact['policy_version']}`",
        f"- Generated: `{artifact['generated_at']}`",
        "- Decision: **NOT READY — SHADOW ONLY**",
        "- Payload policy: aggregate metadata only; no questions, evidence, or answers persisted.",
        "",
        "## Eligibility waterfall",
        "",
    ]
    for stage in artifact["eligibility_waterfall"]:
        lines.append(f"- `{stage['stage']}`: {stage['case_count']}")
    lines.extend([
        "",
        "## Scope",
        "",
        f"- Qualifying observations: {scope['qualifying_observation_count']}",
        f"- Repair cases / controls: {scope['repair_case_count']} / {scope['no_repair_control_count']}",
        f"- Projected skips: {scope['projected_skip_case_count']}",
        f"- Organic real-user cases: {scope['organic_real_user_case_count']}",
        "",
        "## Failure clusters",
        "",
    ])
    for cluster in artifact["clusters"]:
        metrics = cluster["metrics"]
        lines.extend([
            f"### `{cluster['cluster_code']}`",
            "",
            f"- First blocker: `{cluster['policy_reason']}`",
            f"- Cases: {metrics['case_count']} ({metrics['share_of_repair_cases']:.1%} of repair cases)",
            f"- Retention p50 / p95: {metrics['substantive_line_retention']['p50']} / {metrics['substantive_line_retention']['p95']}",
            f"- Shadow compute p95: {metrics['shadow_compute_ms']['p95']} ms",
            f"- Interpretation: {cluster['interpretation']}",
            "",
        ])
        diagnosis = cluster.get("reviewed_safety_diagnosis")
        if diagnosis:
            lines.extend([
                "Reviewed safety subset:",
                "",
                f"- Matched cases: {diagnosis['matched_semantic_cluster_case_count']}",
                f"- Cluster coverage: {diagnosis['semantic_cluster_case_coverage']:.1%}",
                f"- Issue counts: `{json.dumps(diagnosis['issue_counts'], sort_keys=True)}`",
                f"- Root-cause counts: `{json.dumps(diagnosis['root_cause_counts'], sort_keys=True)}`",
                f"- Caveat: {diagnosis['interpretation']}",
                "",
            ])
    lines.extend(["## Policy v3 design options (not implemented)", ""])
    for option in artifact["policy_v3_design_options"]:
        lines.extend([
            f"### `{option['option_id']}`",
            "",
            f"- Target: `{option['target_cluster']}`",
            f"- Hypothesis: {option['design_hypothesis']}",
            f"- Expected benefit: {option['expected_benefit']}",
            f"- Risks: `{json.dumps(option['primary_risks'])}`",
            "",
        ])
    lines.extend([
        "## Decision boundary",
        "",
        "No policy, context, loop, serving, or promotion change is authorized by this report. "
        "Policy v2 remains shadow-only, and controlled evidence must not be relabeled as organic.",
        "",
        f"Content hash: `{artifact['content_hash']}`",
    ])
    return "\n".join(lines).rstrip() + "\n"


def write_failure_cluster_report(
    artifact: dict[str, Any],
    json_path: Path,
    markdown_path: Path | None = None,
) -> tuple[Path, Path]:
    if artifact.get("content_hash") != content_hash(artifact):
        raise ValueError("failure-cluster artifact content hash is invalid")
    json_destination = Path(json_path).resolve()
    markdown_destination = (
        Path(markdown_path).resolve()
        if markdown_path is not None
        else json_destination.with_suffix(".md")
    )
    json_destination.parent.mkdir(parents=True, exist_ok=True)
    markdown_destination.parent.mkdir(parents=True, exist_ok=True)
    json_temp = json_destination.with_suffix(json_destination.suffix + ".tmp")
    md_temp = markdown_destination.with_suffix(markdown_destination.suffix + ".tmp")
    json_temp.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    md_temp.write_text(
        render_failure_cluster_markdown(artifact), encoding="utf-8", newline="\n"
    )
    json_temp.replace(json_destination)
    md_temp.replace(markdown_destination)
    return json_destination, markdown_destination


__all__ = ["render_failure_cluster_markdown", "write_failure_cluster_report"]
