"""Rendering and atomic writes for Policy-v3 selector experiment reports."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.assistant.evals.artifacts import content_hash


def render_selector_experiment_markdown(artifact: dict[str, Any]) -> str:
    split = artifact["split"]
    train = artifact["train_metrics"]
    holdout = artifact["holdout_metrics"]
    combined = artifact["combined_metrics"]
    performance = artifact["performance"]["selector_inference_including_features_ms"]
    lines = [
        "# Grounding Repair Policy v3 Hash-Split Selector Experiment",
        "",
        f"- Cohort: `{artifact['cohort_id']}`",
        f"- Experiment: `{artifact['experiment_version']}`",
        f"- Generated: `{artifact['generated_at']}`",
        "- Mode: **OFFLINE CONTROLLED SHADOW RESEARCH ONLY**",
        "- Payload policy: aggregate metadata only; no feature rows, questions, evidence, or answers persisted.",
        "",
        "## Deterministic split",
        "",
        f"- Grouping key: `{split['grouping_key']}`",
        f"- Train / holdout cases: {split['train_case_count']} / {split['holdout_case_count']}",
        f"- Train / holdout request groups: {split['train_request_group_count']} / {split['holdout_request_group_count']}",
        f"- Request-group overlap: {split['request_group_overlap_count']}",
        f"- Strict-oracle positives: {split['train_strict_oracle_positive_count']} / {split['holdout_strict_oracle_positive_count']}",
        "",
        "## Locked selector",
        "",
        f"- Method: `{artifact['training']['method']}`",
        f"- Selected rule: `{json.dumps(artifact['training']['selected_rule'], sort_keys=True)}`",
        f"- Candidate clauses considered: {artifact['training']['zero_false_positive_clause_count']}",
        "",
        "## Train metrics",
        "",
        f"- Selected / cases: {train['selected_case_count']} / {train['case_count']}",
        f"- True positives / false positives: {train['true_positive_count']} / {train['false_positive_count']}",
        f"- Precision / recall: {train['precision']:.2%} / {train['recall']:.2%}",
        "",
        "## Holdout metrics",
        "",
        f"- Selected / cases: {holdout['selected_case_count']} / {holdout['case_count']}",
        f"- True positives / false positives: {holdout['true_positive_count']} / {holdout['false_positive_count']}",
        f"- Precision / recall: {holdout['precision']:.2%} / {holdout['recall']:.2%}",
        f"- Selected Candidate issue count: {holdout['selected_candidate_issue_count']}",
        "",
        "## Combined controlled projection",
        "",
        f"- Selected / repair cases: {combined['selected_case_count']} / {combined['case_count']}",
        f"- Projected repair-call reduction: {combined['projected_repair_call_reduction']:.2%}",
        f"- Selected cluster counts: `{json.dumps(combined['selected_cluster_counts'], sort_keys=True)}`",
        f"- Median theoretical completion saving: {combined['selected_theoretical_completion_savings_ms']['p50']} ms",
        f"- Selector inference p95 including features: {performance['p95']} ms",
        "",
        "## Predeclared gates",
        "",
    ]
    for key, passed in artifact["gates"].items():
        lines.append(f"- {'PASS' if passed else 'FAIL'} `{key}`")
    lines.extend([
        "",
        "## Decision boundary",
        "",
        f"Experiment status: `{artifact['decision']['status']}`.",
        "This controlled holdout result cannot authorize policy, context, loop, runtime, serving, "
        "or promotion changes. Controlled simulation must not be relabeled as organic evidence.",
        "",
        f"Content hash: `{artifact['content_hash']}`",
    ])
    return "\n".join(lines).rstrip() + "\n"


def write_selector_experiment_report(
    artifact: dict[str, Any],
    json_path: Path,
    markdown_path: Path | None = None,
) -> tuple[Path, Path]:
    if artifact.get("content_hash") != content_hash(artifact):
        raise ValueError("selector experiment artifact content hash is invalid")
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
        render_selector_experiment_markdown(artifact),
        encoding="utf-8",
        newline="\n",
    )
    json_temp.replace(json_destination)
    markdown_temp.replace(markdown_destination)
    return json_destination, markdown_destination


__all__ = ["render_selector_experiment_markdown", "write_selector_experiment_report"]
