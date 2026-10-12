"""Strict nightly regression checks for the AI Workspace evaluation suite."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from statistics import fmean
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .artifacts import verify_content_hash
from .models import EvalModel, EvalRunResult, SuiteManifest


class NightlyBaseline(EvalModel):
    """Versioned acceptance policy for one private nightly evaluation corpus."""

    schema_version: Literal[1] = 1
    baseline_id: str = Field(min_length=3)
    suite_id: str = Field(min_length=3)
    suite_kind: str = "frozen_ci"
    manifest_content_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    fixture_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    case_ids: list[str] = Field(min_length=1)
    repetitions_per_case: int = Field(default=3, ge=1, le=20)
    minimum_case_pass_rate: float = Field(default=1.0, ge=0.0, le=1.0)
    minimum_repetition_pass_rate: float = Field(default=1.0, ge=0.0, le=1.0)
    minimum_mean_score: float = Field(default=95.0, ge=0.0, le=100.0)
    maximum_flaky_cases: int = Field(default=0, ge=0)
    maximum_judge_errors: int = Field(default=0, ge=0)
    maximum_candidate_errors: int = Field(default=0, ge=0)
    maximum_validation_errors: int = Field(default=0, ge=0)
    reference_run_id: str = Field(min_length=3)
    reference_run_content_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    reference_completed_at: datetime
    reference_repetition_passes: int = Field(ge=0)
    reference_total_repetitions: int = Field(ge=1)
    reference_mean_score: float = Field(ge=0.0, le=100.0)
    content_hash: str = ""

    @field_validator("case_ids")
    @classmethod
    def unique_case_ids(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value if item.strip()]
        if len(normalized) != len(set(normalized)):
            raise ValueError("case_ids must be unique")
        return normalized

    @field_validator("reference_completed_at")
    @classmethod
    def require_aware_reference_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("reference_completed_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_reference_counts(self) -> "NightlyBaseline":
        expected = len(self.case_ids) * self.repetitions_per_case
        if self.reference_total_repetitions != expected:
            raise ValueError("reference_total_repetitions must match cases x repetitions")
        if self.reference_repetition_passes > self.reference_total_repetitions:
            raise ValueError("reference repetition passes cannot exceed total repetitions")
        return self


class RegressionCheck(EvalModel):
    check_id: str
    passed: bool
    expected: object | None = None
    actual: object | None = None
    detail: str = ""


class RegressionMetrics(EvalModel):
    total_cases: int = Field(ge=0)
    passed_cases: int = Field(ge=0)
    case_pass_rate: float = Field(ge=0.0, le=1.0)
    total_repetitions: int = Field(ge=0)
    passed_repetitions: int = Field(ge=0)
    repetition_pass_rate: float = Field(ge=0.0, le=1.0)
    mean_score: float = Field(ge=0.0, le=100.0)
    flaky_cases: int = Field(ge=0)
    judge_errors: int = Field(ge=0)
    candidate_errors: int = Field(ge=0)
    validation_errors: int = Field(ge=0)


class NightlyRegressionSummary(EvalModel):
    schema_version: Literal[1] = 1
    baseline_id: str
    generated_at: datetime
    phase: Literal["preflight", "run"]
    passed: bool
    run_id: str | None = None
    run_result_path: str | None = None
    report_directory: str | None = None
    metrics: RegressionMetrics | None = None
    checks: list[RegressionCheck] = Field(default_factory=list)

    @field_validator("generated_at")
    @classmethod
    def require_aware_generated_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("generated_at must be timezone-aware")
        return value


def sha256_file(path: Path) -> str:
    """Return a prefixed SHA-256 digest without loading a large fixture into memory."""

    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _check(
    check_id: str,
    passed: bool,
    *,
    expected: object | None = None,
    actual: object | None = None,
    detail: str = "",
) -> RegressionCheck:
    return RegressionCheck(
        check_id=check_id,
        passed=passed,
        expected=expected,
        actual=actual,
        detail=detail,
    )


def evaluate_preflight(
    baseline: NightlyBaseline,
    manifest: SuiteManifest,
    fixture_path: Path,
) -> tuple[list[RegressionCheck], str | None]:
    """Reject corpus drift before spending time or money on live LLM calls."""

    fixture_exists = fixture_path.is_file()
    fixture_hash = sha256_file(fixture_path) if fixture_exists else None
    manifest_case_ids = [reference.case_id for reference in manifest.cases]
    checks = [
        _check(
            "baseline_content_hash",
            verify_content_hash(baseline),
            expected="valid content hash",
            actual=baseline.content_hash,
        ),
        _check(
            "manifest_content_hash",
            verify_content_hash(manifest),
            expected="valid content hash",
            actual=manifest.content_hash,
        ),
        _check(
            "suite_id",
            manifest.suite_id == baseline.suite_id,
            expected=baseline.suite_id,
            actual=manifest.suite_id,
        ),
        _check(
            "suite_kind",
            manifest.kind.value == baseline.suite_kind,
            expected=baseline.suite_kind,
            actual=manifest.kind.value,
        ),
        _check(
            "pinned_manifest_hash",
            manifest.content_hash == baseline.manifest_content_hash,
            expected=baseline.manifest_content_hash,
            actual=manifest.content_hash,
            detail="Update the baseline only after human review and a successful reference run.",
        ),
        _check(
            "pinned_case_ids",
            manifest_case_ids == baseline.case_ids,
            expected=baseline.case_ids,
            actual=manifest_case_ids,
        ),
        _check(
            "fixture_exists",
            fixture_exists,
            expected=True,
            actual=fixture_exists,
            detail=str(fixture_path),
        ),
        _check(
            "pinned_fixture_hash",
            fixture_hash == baseline.fixture_sha256,
            expected=baseline.fixture_sha256,
            actual=fixture_hash,
            detail="A changed private fixture requires a reviewed baseline revision.",
        ),
    ]
    return checks, fixture_hash


def build_preflight_summary(
    baseline: NightlyBaseline,
    checks: list[RegressionCheck],
) -> NightlyRegressionSummary:
    return NightlyRegressionSummary(
        baseline_id=baseline.baseline_id,
        generated_at=datetime.now(timezone.utc),
        phase="preflight",
        passed=all(check.passed for check in checks),
        checks=checks,
    )


def evaluate_nightly_run(
    baseline: NightlyBaseline,
    result: EvalRunResult,
    *,
    preflight_checks: list[RegressionCheck] | None = None,
    run_result_path: Path | None = None,
    report_directory: Path | None = None,
) -> NightlyRegressionSummary:
    """Apply strict no-regression gates to a completed 3x live run."""

    cases = result.cases
    actual_case_ids = [case.case_id for case in cases]
    total_repetitions = sum(len(case.repetitions) for case in cases)
    passed_repetitions = sum(
        1 for case in cases for repetition in case.repetitions if repetition.overall_pass
    )
    scores = [
        float(repetition.score_0_to_100)
        for case in cases
        for repetition in case.repetitions
    ]
    passed_cases = sum(1 for case in cases if case.overall_pass)
    flaky_cases = sum(1 for case in cases if case.flaky)
    judge_errors = sum(
        1 for case in cases for repetition in case.repetitions if repetition.judge_error
    )
    candidate_errors = sum(
        1 for case in cases for candidate in case.candidates if candidate.error
    )
    validation_errors = sum(
        len(candidate.validation_errors)
        for case in cases
        for candidate in case.candidates
    )
    total_cases = len(cases)
    metrics = RegressionMetrics(
        total_cases=total_cases,
        passed_cases=passed_cases,
        case_pass_rate=(passed_cases / total_cases) if total_cases else 0.0,
        total_repetitions=total_repetitions,
        passed_repetitions=passed_repetitions,
        repetition_pass_rate=(passed_repetitions / total_repetitions) if total_repetitions else 0.0,
        mean_score=fmean(scores) if scores else 0.0,
        flaky_cases=flaky_cases,
        judge_errors=judge_errors,
        candidate_errors=candidate_errors,
        validation_errors=validation_errors,
    )
    expected_total_repetitions = len(baseline.case_ids) * baseline.repetitions_per_case
    repetition_counts = {case.case_id: len(case.repetitions) for case in cases}
    expected_repetition_counts = {
        case_id: baseline.repetitions_per_case for case_id in baseline.case_ids
    }
    checks = list(preflight_checks or [])
    checks.extend([
        _check(
            "run_content_hash",
            verify_content_hash(result),
            expected="valid content hash",
            actual=result.content_hash,
        ),
        _check("live_mode", result.mode == "live", expected="live", actual=result.mode),
        _check(
            "run_suite_id",
            result.suite_id == baseline.suite_id,
            expected=baseline.suite_id,
            actual=result.suite_id,
        ),
        _check(
            "run_case_ids",
            actual_case_ids == baseline.case_ids,
            expected=baseline.case_ids,
            actual=actual_case_ids,
        ),
        _check(
            "artifact_hash_coverage",
            set(result.artifact_hashes) == set(baseline.case_ids),
            expected=sorted(baseline.case_ids),
            actual=sorted(result.artifact_hashes),
        ),
        _check(
            "repetitions_per_case",
            repetition_counts == expected_repetition_counts,
            expected=expected_repetition_counts,
            actual=repetition_counts,
        ),
        _check(
            "total_repetitions",
            total_repetitions == expected_total_repetitions,
            expected=expected_total_repetitions,
            actual=total_repetitions,
        ),
        _check(
            "case_pass_rate",
            metrics.case_pass_rate >= baseline.minimum_case_pass_rate,
            expected=f">= {baseline.minimum_case_pass_rate:.2%}",
            actual=f"{metrics.case_pass_rate:.2%}",
        ),
        _check(
            "repetition_pass_rate",
            metrics.repetition_pass_rate >= baseline.minimum_repetition_pass_rate,
            expected=f">= {baseline.minimum_repetition_pass_rate:.2%}",
            actual=f"{metrics.repetition_pass_rate:.2%}",
        ),
        _check(
            "mean_score",
            metrics.mean_score >= baseline.minimum_mean_score,
            expected=f">= {baseline.minimum_mean_score:.2f}",
            actual=f"{metrics.mean_score:.2f}",
        ),
        _check(
            "flaky_cases",
            metrics.flaky_cases <= baseline.maximum_flaky_cases,
            expected=f"<= {baseline.maximum_flaky_cases}",
            actual=metrics.flaky_cases,
        ),
        _check(
            "judge_errors",
            metrics.judge_errors <= baseline.maximum_judge_errors,
            expected=f"<= {baseline.maximum_judge_errors}",
            actual=metrics.judge_errors,
        ),
        _check(
            "candidate_errors",
            metrics.candidate_errors <= baseline.maximum_candidate_errors,
            expected=f"<= {baseline.maximum_candidate_errors}",
            actual=metrics.candidate_errors,
        ),
        _check(
            "validation_errors",
            metrics.validation_errors <= baseline.maximum_validation_errors,
            expected=f"<= {baseline.maximum_validation_errors}",
            actual=metrics.validation_errors,
        ),
    ])
    return NightlyRegressionSummary(
        baseline_id=baseline.baseline_id,
        generated_at=datetime.now(timezone.utc),
        phase="run",
        passed=all(check.passed for check in checks),
        run_id=result.run_id,
        run_result_path=str(run_result_path) if run_result_path else None,
        report_directory=str(report_directory) if report_directory else None,
        metrics=metrics,
        checks=checks,
    )


def render_regression_markdown(summary: NightlyRegressionSummary) -> str:
    """Render a compact CI summary while the full eval report retains case details."""

    status = "PASS" if summary.passed else "FAIL"
    lines = [
        "# AI Workspace Nightly Regression",
        "",
        f"- Baseline: `{summary.baseline_id}`",
        f"- Phase: `{summary.phase}`",
        f"- Result: **{status}**",
    ]
    if summary.run_id:
        lines.append(f"- Run: `{summary.run_id}`")
    if summary.metrics:
        metrics = summary.metrics
        lines.extend([
            f"- Cases: **{metrics.passed_cases}/{metrics.total_cases}**",
            f"- Repetitions: **{metrics.passed_repetitions}/{metrics.total_repetitions}**",
            f"- Mean score: **{metrics.mean_score:.2f}/100**",
            f"- Flaky cases: **{metrics.flaky_cases}**",
            f"- Judge errors: **{metrics.judge_errors}**",
            f"- Candidate errors: **{metrics.candidate_errors}**",
            f"- Validation errors: **{metrics.validation_errors}**",
        ])
    lines.extend(["", "## Gates", "", "| Gate | Result | Expected | Actual |", "|---|---:|---|---|"])
    for check in summary.checks:
        gate_status = "PASS" if check.passed else "FAIL"
        expected = str(check.expected if check.expected is not None else "").replace("|", "\\|")
        actual = str(check.actual if check.actual is not None else "").replace("|", "\\|")
        lines.append(f"| `{check.check_id}` | **{gate_status}** | {expected} | {actual} |")
    failed = [check for check in summary.checks if not check.passed]
    if failed:
        lines.extend(["", "## Failed gate details", ""])
        for check in failed:
            detail = check.detail or "See the full eval report and run artifact."
            lines.append(f"- `{check.check_id}`: {detail}")
    return "\n".join(lines) + "\n"
