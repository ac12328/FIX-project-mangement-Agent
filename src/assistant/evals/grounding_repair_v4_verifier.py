"""Independent verifier for Policy-v4 grounding-repair research artifacts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, get_args

from config.settings import AIWorkspaceConfig
from src.assistant.evals.artifacts import read_model, write_model
from src.assistant.evals.grounding_repair_v4_experiment import run_experiment_v4
from src.assistant.evals.grounding_repair_v4_protocol import (
    AutomatedReviewBatch,
    EncryptedQuestionSet,
    ExperimentReport,
    HistoricalExclusionManifest,
    HoldoutCollection,
    Preregistration,
    QuestionManifest,
    ResearchScreenStatus,
    SelectionManifest,
    VerificationReport,
    normalized_request_hash,
    sha256_text,
    stamp_v4,
    verify_v4,
)
from src.assistant.evals.grounding_repair_v4_trace_lifecycle import lifecycle_payload_valid
from src.observability.payload_crypto import decrypt_payload

_FORBIDDEN_KEYS = frozenset({
    "question", "request_text", "prepared_answer", "candidate_answer",
    "baseline_answer", "reference_answer", "answer_a", "answer_b",
    "evidence", "sources", "tool_results", "rationale", "rationales",
    "feature_row", "feature_rows", "payload_plaintext", "prompt_text",
    "response_text",
})
_DETERMINISTIC_METRICS = frozenset({
    "holdout_case_count", "complete_repair_case_count", "selected_case_count",
    "distinct_request_hash_count", "automated_review_complete_count",
    "automated_review_coverage", "replay_complete_count",
    "candidate_answer_parity_count", "initial_error_parity_count",
    "candidate_error_parity_count", "certificate_selected_count",
    "projected_repair_reduction", "false_positive_count",
    "selected_candidate_issue_count",
})


def _walk_sensitive_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        normalized = value.strip()
        if len(normalized) >= 32:
            yield normalized
    elif isinstance(value, dict):
        for child in value.values():
            yield from _walk_sensitive_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_sensitive_strings(child)


def _forbidden_key_hits(value: Any, path: str = "$") -> list[str]:
    hits: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if str(key).casefold() in _FORBIDDEN_KEYS:
                hits.append(child_path)
            hits.extend(_forbidden_key_hits(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            hits.extend(_forbidden_key_hits(child, f"{path}[{index}]"))
    return hits


def _file_format_valid(path: Path) -> bool:
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return (
        not raw.startswith(b"\xef\xbb\xbf")
        and b"\r" not in raw
        and text.endswith("\n")
        and all(line == line.rstrip() for line in text.splitlines())
    )


def _question_checks(
    historical: HistoricalExclusionManifest,
    manifest: QuestionManifest,
    encrypted: EncryptedQuestionSet,
) -> tuple[dict[str, bool], list[str]]:
    checks = {
        "encrypted_question_lineage": (
            manifest.encrypted_payload_artifact_hash == encrypted.content_hash
            and encrypted.question_set_id == manifest.question_set_id
        ),
        "question_manifest_payload_match": True,
        "historical_request_overlap_zero": True,
    }
    needles: list[str] = []
    historical_hashes = set(historical.request_hashes)
    for entry, index in zip(encrypted.entries, manifest.questions, strict=True):
        try:
            payload = decrypt_payload(
                entry.payload.ciphertext, entry.payload.nonce, entry.payload.key_version
            )
        except Exception:
            checks["question_manifest_payload_match"] = False
            continue
        if not isinstance(payload, dict):
            checks["question_manifest_payload_match"] = False
            continue
        question = str(payload.get("question") or "")
        matches = (
            entry.order == index.order
            and entry.question_id == index.question_id
            and entry.payload.payload_hash == index.encrypted_payload_hash
            and sha256_text(question) == index.question_hash
            and normalized_request_hash(question) == index.request_hash
            and payload.get("question_hash") == index.question_hash
            and payload.get("request_hash") == index.request_hash
            and payload.get("web_enabled") is False
        )
        checks["question_manifest_payload_match"] &= matches
        checks["historical_request_overlap_zero"] &= index.request_hash not in historical_hashes
        needles.extend(_walk_sensitive_strings(payload.get("question")))
    return checks, needles


def _holdout_checks(
    manifest: QuestionManifest,
    holdout: HoldoutCollection,
    selection: SelectionManifest,
) -> tuple[dict[str, bool], list[str]]:
    pairs_match = all(
        case.sealed_order == index.order
        and case.question_id == index.question_id
        and case.question_hash == index.question_hash
        and case.request_hash == index.request_hash
        for case, index in zip(holdout.cases, manifest.questions, strict=True)
    )
    eligible = [item for item in holdout.cases if item.complete and item.repair_triggered]
    expected = eligible[:200]
    selection_matches = (
        selection.eligible_repair_case_count == len(eligible)
        and selection.selected_case_count == len(expected)
        and selection.status == ("complete" if len(eligible) >= 200 else "insufficient_sample")
        and selection.selected_question_hashes == [item.question_hash for item in expected]
        and selection.selected_case_payload_hashes == [
            str(item.case_payload_hash) for item in expected
        ]
    )
    needles: list[str] = []
    payloads_valid = True
    completed_lifecycles_valid = True
    for case in holdout.cases:
        if case.encrypted_case_payload is None:
            continue
        blob = case.encrypted_case_payload
        try:
            payload = decrypt_payload(blob.ciphertext, blob.nonce, blob.key_version)
        except Exception:
            payloads_valid = False
            continue
        if not isinstance(payload, dict):
            payloads_valid = False
            continue
        serialized = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        payloads_valid &= (
            hashlib.sha256(serialized).hexdigest() == blob.payload_hash
            and case.case_payload_hash == blob.payload_hash
            and payload.get("question_hash") == case.question_hash
            and payload.get("request_hash") == case.request_hash
        )
        if case.complete:
            completed_lifecycles_valid &= lifecycle_payload_valid(
                payload.get("trace_lifecycle")
            )
        for sensitive_field in (
            "question", "prepared_answer", "candidate_answer", "baseline_answer",
            "sources", "tool_results", "rationale", "rationales",
        ):
            needles.extend(_walk_sensitive_strings(payload.get(sensitive_field)))
    all_sessions_revoked = (
        holdout.temporary_session_count == holdout.temporary_session_revoked_count
        and all(item.temporary_session_revoked for item in holdout.cases)
    )
    all_threads_archived = (
        holdout.thread_created_count == holdout.thread_archived_count
        and all(item.thread_created_count == item.thread_archived_count for item in holdout.cases)
    )
    completed = [item for item in holdout.cases if item.complete]
    serving_invariants = all(
        item.served_answer_legacy
        and item.user_visible_behavior_unchanged
        and item.business_persistence_unchanged
        and item.thread_archived
        for item in completed
    )
    return {
        "manifest_holdout_order_and_hash_match": pairs_match,
        "first_200_sealed_selection_rule_match": selection_matches,
        "encrypted_case_payload_lineage_valid": payloads_valid,
        "completed_case_trace_lifecycle_valid": completed_lifecycles_valid,
        "business_fingerprints_unchanged": (
            holdout.business_persistence_unchanged
            and holdout.business_fingerprint_before == holdout.business_fingerprint_after
        ),
        "temporary_sessions_all_revoked": all_sessions_revoked,
        "threads_all_archived": all_threads_archived,
        "completed_case_serving_invariants": serving_invariants,
    }, needles


def _expected_status(gates: dict[str, bool]) -> ResearchScreenStatus:
    incomplete = (
        "input_artifacts_valid", "lineage_valid", "selection_complete",
        "complete_repair_case_target_met", "distinct_request_target_met",
        "automated_review_coverage_met", "replay_candidate_and_error_parity",
    )
    safety = ("false_positive_limit_met", "selected_candidate_issue_limit_met")
    if not all(gates.get(name, False) for name in incomplete):
        return ResearchScreenStatus.INCOMPLETE
    if not all(gates.get(name, False) for name in safety):
        return ResearchScreenStatus.FAILED_SAFETY
    if not all(gates.values()):
        return ResearchScreenStatus.INCONCLUSIVE
    return ResearchScreenStatus.PASSED


def verify_experiment_v4(
    *,
    historical_path: Path,
    manifest_path: Path,
    encrypted_questions_path: Path,
    preregistration_path: Path,
    holdout_path: Path,
    selection_path: Path,
    reviews_path: Path,
    report_path: Path,
    output_path: Path | None = None,
    grounding_path: Path | None = None,
) -> VerificationReport:
    historical = read_model(Path(historical_path), HistoricalExclusionManifest)
    manifest = read_model(Path(manifest_path), QuestionManifest)
    encrypted = read_model(Path(encrypted_questions_path), EncryptedQuestionSet)
    prereg = read_model(Path(preregistration_path), Preregistration)
    holdout = read_model(Path(holdout_path), HoldoutCollection)
    selection = read_model(Path(selection_path), SelectionManifest)
    reviews = read_model(Path(reviews_path), AutomatedReviewBatch)
    report = read_model(Path(report_path), ExperimentReport)
    paths = [
        Path(historical_path), Path(manifest_path), Path(encrypted_questions_path),
        Path(preregistration_path), Path(holdout_path), Path(selection_path),
        Path(reviews_path), Path(report_path),
    ]
    checks: dict[str, bool] = {
        "all_artifact_hashes_valid": all(verify_v4(item) for item in (
            historical, manifest, encrypted, prereg, holdout, selection, reviews, report
        )),
        "artifact_files_canonical": all(_file_format_valid(path) for path in paths),
        "artifact_lineage_valid": (
            manifest.historical_exclusion_hash == historical.content_hash
            and prereg.question_manifest_hash == manifest.content_hash
            and prereg.historical_exclusion_hash == historical.content_hash
            and holdout.question_manifest_hash == manifest.content_hash
            and holdout.preregistration_hash == prereg.content_hash
            and selection.holdout_hash == holdout.content_hash
            and reviews.selection_hash == selection.content_hash
            and report.preregistration_hash == prereg.content_hash
            and report.question_manifest_hash == manifest.content_hash
            and report.historical_exclusion_hash == historical.content_hash
            and report.holdout_hash == holdout.content_hash
            and report.selection_hash == selection.content_hash
            and report.review_hash == reviews.content_hash
            and report.experiment_id == prereg.experiment_id == holdout.experiment_id
            == selection.experiment_id == reviews.experiment_id
        ),
    }
    question_checks, question_needles = _question_checks(historical, manifest, encrypted)
    holdout_checks, case_needles = _holdout_checks(manifest, holdout, selection)
    checks.update(question_checks)
    checks.update(holdout_checks)

    recomputed = run_experiment_v4(
        historical=historical, manifest=manifest, preregistration=prereg,
        holdout=holdout, selection=selection, reviews=reviews,
        generated_at=report.generated_at,
    )
    checks["deterministic_metrics_reproduced"] = all(
        recomputed.metrics.get(name) == report.metrics.get(name)
        for name in _DETERMINISTIC_METRICS
    )
    checks["reported_timing_gates_match_thresholds"] = (
        report.gates.get("certificate_p95_met")
        == (float(report.metrics.get("certificate_p95_ms") or 0) < prereg.gates.maximum_certificate_p95_ms)
        and report.gates.get("full_replay_p95_met")
        == (float(report.metrics.get("full_replay_p95_ms") or 0) < prereg.gates.maximum_full_replay_p95_ms)
    )
    checks["report_status_matches_gates"] = report.status == _expected_status(report.gates)
    checks["research_only_no_serving_authorization"] = (
        report.research_only is True and report.serving_change_authorized is False
        and prereg.research_only is True and prereg.serving_change_authorized is False
    )

    serialized_files = [path.read_text(encoding="utf-8") for path in paths]
    needles = set(question_needles + case_needles)
    checks["sensitive_plaintext_absent"] = not any(
        needle in text for needle in needles for text in serialized_files
    )
    checks["forbidden_plaintext_structures_absent"] = all(
        not _forbidden_key_hits(json.loads(text)) for text in serialized_files
    )
    runtime_path = Path(grounding_path) if grounding_path else (
        Path(__file__).resolve().parents[1] / "grounding.py"
    )
    runtime_source = runtime_path.read_text(encoding="utf-8")
    checks["policy_v4_runtime_import_isolated"] = (
        "grounding_repair_certificate_v4" not in runtime_source
        and "grounding_repair_v4" not in runtime_source
        and "Policy-v4" not in runtime_source
    )
    mode_field = AIWorkspaceConfig.model_fields["grounding_repair_mode"]
    supported_modes = set(get_args(mode_field.annotation))
    checks["production_mode_contract_explicit"] = supported_modes == {
        "legacy", "shadow", "serve"
    }
    checks["production_mode_default_is_non_serving"] = (
        mode_field.default == "legacy"
        and AIWorkspaceConfig().grounding_repair_mode == "legacy"
    )
    checks["production_serve_requires_runtime_configuration"] = (
        AIWorkspaceConfig(grounding_repair_mode="serve").grounding_repair_mode == "serve"
        and report.serving_change_authorized is False
        and prereg.serving_change_authorized is False
    )

    failure_codes = sorted(name for name, passed in checks.items() if not passed)
    verification = stamp_v4(VerificationReport(
        experiment_id=report.experiment_id,
        generated_at=report.generated_at,
        report_hash=report.content_hash,
        checks=checks,
        all_passed=not failure_codes,
        failure_codes=failure_codes,
    ))
    if output_path is not None:
        write_model(Path(output_path), verification)
    return verification


__all__ = ["verify_experiment_v4"]