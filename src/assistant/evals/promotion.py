"""Promote reviewed AI Workspace eval artifacts into immutable frozen fixtures."""

from __future__ import annotations

import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .artifacts import (
    canonical_json,
    lint_artifact_set,
    load_artifact_set,
    read_model,
    stamp_content_hash,
    utc_now,
    verify_content_hash,
    write_model,
)
from .models import (
    ArtifactLifecycle,
    ArtifactRef,
    EvalCase,
    EvidenceBundle,
    GoldenSpec,
    PromotionResult,
    ReviewMetadata,
    SuiteKind,
    SuiteManifest,
)

_ARTIFACT_FILENAMES = ("case.json", "evidence.json", "golden.json", "review.json")
_PROMOTABLE_LIFECYCLES = {
    ArtifactLifecycle.GENERATED,
    ArtifactLifecycle.EVIDENCE_READY,
    ArtifactLifecycle.GOLDEN_READY,
    ArtifactLifecycle.VALIDATED,
}


class ArtifactValidationError(ValueError):
    """Raised when an artifact set or manifest reference fails integrity checks."""


class FrozenArtifactConflictError(FileExistsError):
    """Raised when a frozen destination exists with different immutable content."""


def _artifact_hashes(
    case: EvalCase,
    evidence: EvidenceBundle,
    golden: GoldenSpec,
) -> dict[str, str]:
    return {
        "case": case.content_hash,
        "evidence": evidence.content_hash,
        "golden": golden.content_hash,
    }


def _blocking_issue_message(issues: list[Any]) -> str:
    return "; ".join(
        f"{issue.code} ({issue.path or 'artifact'}): {issue.message}"
        for issue in issues
        if issue.blocking
    )


def _validate_artifacts(
    case: EvalCase,
    evidence: EvidenceBundle,
    golden: GoldenSpec,
    *,
    require_frozen: bool,
) -> None:
    issues = lint_artifact_set(
        case,
        evidence,
        golden,
        require_frozen=require_frozen,
    )
    blocking = [issue for issue in issues if issue.blocking]
    if blocking:
        raise ArtifactValidationError(_blocking_issue_message(blocking))


def _resolve_reference_path(artifact_root: Path, reference_path: str) -> Path:
    root = artifact_root.resolve()
    raw = Path(reference_path)
    resolved = raw.resolve() if raw.is_absolute() else (root / raw).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ArtifactValidationError(
            f"Artifact reference escapes the configured root: {reference_path}"
        ) from exc
    return resolved


def validate_artifact_reference(
    reference: ArtifactRef,
    artifact_root: Path,
    *,
    require_frozen: bool = True,
) -> Path:
    """Validate one manifest reference and return its resolved case directory."""
    case_dir = _resolve_reference_path(Path(artifact_root), reference.path)
    try:
        case, evidence, golden = load_artifact_set(case_dir)
    except Exception as exc:
        raise ArtifactValidationError(
            f"Unable to load artifact reference {reference.case_id} v{reference.version}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    _validate_artifacts(
        case,
        evidence,
        golden,
        require_frozen=require_frozen,
    )
    if case.case_id != reference.case_id or case.version != reference.version:
        raise ArtifactValidationError(
            "Manifest reference identity does not match the loaded case: "
            f"expected {reference.case_id} v{reference.version}, "
            f"found {case.case_id} v{case.version}"
        )

    expected_hashes = {
        "case": reference.case_hash,
        "evidence": reference.evidence_hash,
        "golden": reference.golden_hash,
    }
    actual_hashes = _artifact_hashes(case, evidence, golden)
    mismatches = {
        name: {"expected": expected_hashes[name], "actual": actual_hashes[name]}
        for name in expected_hashes
        if expected_hashes[name] != actual_hashes[name]
    }
    if mismatches:
        raise ArtifactValidationError(
            f"Manifest reference hashes do not match {case_dir}: {mismatches}"
        )
    return case_dir


def validate_manifest_references(
    manifest: SuiteManifest,
    artifact_root: Path,
    *,
    require_manifest_hash: bool = True,
) -> dict[str, Path]:
    """Validate manifest integrity and every content-addressed artifact reference."""
    if require_manifest_hash and not verify_content_hash(manifest):
        raise ArtifactValidationError("Suite manifest content_hash is missing or invalid")
    if manifest.minimum_passes > manifest.repetitions:
        raise ArtifactValidationError(
            "Suite manifest minimum_passes cannot exceed repetitions"
        )

    resolved: dict[str, Path] = {}
    identities: set[tuple[str, int]] = set()
    paths: set[Path] = set()
    require_frozen = manifest.kind != SuiteKind.GENERATED
    for reference in manifest.cases:
        identity = (reference.case_id, reference.version)
        if identity in identities:
            raise ArtifactValidationError(
                f"Suite manifest contains duplicate reference {identity[0]} v{identity[1]}"
            )
        case_dir = validate_artifact_reference(
            reference,
            artifact_root,
            require_frozen=require_frozen,
        )
        if case_dir in paths:
            raise ArtifactValidationError(
                f"Suite manifest points multiple references at {case_dir}"
            )
        identities.add(identity)
        paths.add(case_dir)
        resolved[f"{reference.case_id}:v{reference.version}"] = case_dir
    return resolved


def _read_review(path: Path) -> ReviewMetadata:
    try:
        review = read_model(path, ReviewMetadata)
    except Exception as exc:
        raise FrozenArtifactConflictError(
            f"Frozen review metadata is missing or invalid at {path}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    if not verify_content_hash(review):
        raise FrozenArtifactConflictError(
            f"Frozen review metadata content_hash is invalid at {path}"
        )
    return review


def _same_model(left: Any, right: Any) -> bool:
    return canonical_json(left) == canonical_json(right)


def _validate_existing_destination(
    destination: Path,
    *,
    frozen_case: EvalCase,
    evidence: EvidenceBundle,
    golden: GoldenSpec,
    reviewed_by: str,
    review_notes: str,
    reviewed_at: datetime | None,
    source_hashes: dict[str, str],
) -> PromotionResult:
    try:
        existing_case, existing_evidence, existing_golden = load_artifact_set(destination)
        _validate_artifacts(
            existing_case,
            existing_evidence,
            existing_golden,
            require_frozen=True,
        )
    except FrozenArtifactConflictError:
        raise
    except Exception as exc:
        raise FrozenArtifactConflictError(
            f"Frozen destination is incomplete or invalid at {destination}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    expected_models = (frozen_case, evidence, golden)
    existing_models = (existing_case, existing_evidence, existing_golden)
    if any(
        not _same_model(expected, existing)
        for expected, existing in zip(expected_models, existing_models)
    ):
        raise FrozenArtifactConflictError(
            f"Refusing to overwrite frozen artifacts with different content: {destination}"
        )

    existing_review = _read_review(destination / "review.json")
    effective_reviewed_at = reviewed_at or existing_review.reviewed_at
    expected_review = stamp_content_hash(
        ReviewMetadata(
            case_id=frozen_case.case_id,
            case_version=frozen_case.version,
            reviewed_by=reviewed_by,
            review_notes=review_notes,
            reviewed_at=effective_reviewed_at,
            source_artifact_hashes=source_hashes,
        )
    )
    if not _same_model(expected_review, existing_review):
        raise FrozenArtifactConflictError(
            f"Refusing to overwrite frozen review metadata with different content: "
            f"{destination / 'review.json'}"
        )

    return PromotionResult(
        case_id=frozen_case.case_id,
        version=frozen_case.version,
        destination=str(destination),
        promoted=False,
        idempotent=True,
        artifact_hashes={
            **_artifact_hashes(existing_case, existing_evidence, existing_golden),
            "review": existing_review.content_hash,
        },
    )


def _cleanup_temporary_directory(path: Path) -> None:
    """Remove only files created by this module, then remove the empty directory."""
    for filename in _ARTIFACT_FILENAMES:
        candidate = path / filename
        if candidate.exists():
            candidate.unlink()
    if path.exists():
        path.rmdir()


def promote_generated_case(
    generated_case_dir: Path,
    frozen_root: Path,
    *,
    reviewed_by: str,
    review_notes: str,
    reviewed_at: datetime | None = None,
    source_reference: ArtifactRef | None = None,
    artifact_root: Path | None = None,
) -> PromotionResult:
    """Promote one reviewed generated artifact set to an immutable frozen version.

    The destination layout is ``<frozen_root>/<case_id>/v<version>``. Repeating
    the same promotion preserves the original review timestamp and returns an
    idempotent result. Any difference in artifacts or review metadata raises
    ``FrozenArtifactConflictError`` rather than overwriting frozen content.
    """
    reviewer = reviewed_by.strip()
    notes = review_notes.strip()
    if not reviewer:
        raise ValueError("reviewed_by is required")
    if not notes:
        raise ValueError("review_notes is required")
    if reviewed_at is not None and (
        reviewed_at.tzinfo is None or reviewed_at.utcoffset() is None
    ):
        raise ValueError("reviewed_at must be timezone-aware")

    source_dir = Path(generated_case_dir).resolve()
    if source_reference is not None:
        if artifact_root is None:
            raise ValueError("artifact_root is required with source_reference")
        referenced_dir = validate_artifact_reference(
            source_reference,
            artifact_root,
            require_frozen=False,
        )
        if referenced_dir != source_dir:
            raise ArtifactValidationError(
                f"Source reference resolves to {referenced_dir}, not {source_dir}"
            )

    try:
        source_case, evidence, golden = load_artifact_set(source_dir)
    except Exception as exc:
        raise ArtifactValidationError(
            f"Unable to load generated artifacts from {source_dir}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    if source_case.lifecycle not in _PROMOTABLE_LIFECYCLES:
        raise ArtifactValidationError(
            f"Case lifecycle {source_case.lifecycle.value!r} cannot be promoted to frozen"
        )
    _validate_artifacts(
        source_case,
        evidence,
        golden,
        require_frozen=False,
    )

    source_hashes = _artifact_hashes(source_case, evidence, golden)
    frozen_case = stamp_content_hash(
        source_case.model_copy(update={"lifecycle": ArtifactLifecycle.FROZEN})
    )
    destination = (
        Path(frozen_root).resolve()
        / frozen_case.case_id
        / f"v{frozen_case.version}"
    )

    if destination.exists():
        return _validate_existing_destination(
            destination,
            frozen_case=frozen_case,
            evidence=evidence,
            golden=golden,
            reviewed_by=reviewer,
            review_notes=notes,
            reviewed_at=reviewed_at,
            source_hashes=source_hashes,
        )

    review = stamp_content_hash(
        ReviewMetadata(
            case_id=frozen_case.case_id,
            case_version=frozen_case.version,
            reviewed_by=reviewer,
            review_notes=notes,
            reviewed_at=reviewed_at or utc_now(),
            source_artifact_hashes=source_hashes,
        )
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.tmp"
    temporary.mkdir(exist_ok=False)
    try:
        write_model(temporary / "case.json", frozen_case, immutable=True)
        write_model(temporary / "evidence.json", evidence, immutable=True)
        write_model(temporary / "golden.json", golden, immutable=True)
        write_model(temporary / "review.json", review, immutable=True)
        try:
            os.replace(temporary, destination)
        except FileExistsError:
            _cleanup_temporary_directory(temporary)
            return _validate_existing_destination(
                destination,
                frozen_case=frozen_case,
                evidence=evidence,
                golden=golden,
                reviewed_by=reviewer,
                review_notes=notes,
                reviewed_at=reviewed_at,
                source_hashes=source_hashes,
            )
    except Exception:
        if temporary.exists():
            _cleanup_temporary_directory(temporary)
        raise

    return PromotionResult(
        case_id=frozen_case.case_id,
        version=frozen_case.version,
        destination=str(destination),
        promoted=True,
        idempotent=False,
        artifact_hashes={
            **_artifact_hashes(frozen_case, evidence, golden),
            "review": review.content_hash,
        },
    )


def promote_case(
    generated_case_dir: Path,
    frozen_root: Path,
    *,
    reviewed_by: str,
    review_notes: str,
    reviewed_at: datetime | None = None,
    source_reference: ArtifactRef | None = None,
    artifact_root: Path | None = None,
) -> PromotionResult:
    """Compatibility alias for ``promote_generated_case``."""
    return promote_generated_case(
        generated_case_dir,
        frozen_root,
        reviewed_by=reviewed_by,
        review_notes=review_notes,
        reviewed_at=reviewed_at,
        source_reference=source_reference,
        artifact_root=artifact_root,
    )


__all__ = [
    "ArtifactValidationError",
    "FrozenArtifactConflictError",
    "promote_case",
    "promote_generated_case",
    "validate_artifact_reference",
    "validate_manifest_references",
]
