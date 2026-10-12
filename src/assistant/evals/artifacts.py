"""Artifact hashing, redaction, loading, and linting."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from .models import EvalCase, EvidenceBundle, GoldenSpec, LintIssue

TModel = TypeVar("TModel", bound=BaseModel)
_CITATION_RE = re.compile(r"\[\[source:([A-Za-z0-9][A-Za-z0-9_.:-]*)\]\]")
_REDACTED_MARKERS = ("[REDACTED_EMAIL]", "[REDACTED_SECRET]", "[REDACTED_PRIVATE_KEY]")
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("email", re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"), "[REDACTED_EMAIL]"),
    ("bearer_token", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}"), "[REDACTED_SECRET]"),
    ("api_key", re.compile(r"\b(?:sk|pk)-[A-Za-z0-9_-]{12,}\b"), "[REDACTED_SECRET]"),
    (
        "secret_assignment",
        re.compile(r"(?i)\b(password|passwd|secret|api[_-]?key)\s*[:=]\s*[^\s,;]{6,}"),
        "[REDACTED_SECRET]",
    ),
    (
        "private_key",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
        "[REDACTED_PRIVATE_KEY]",
    ),
)


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _without_hashes(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_hashes(item)
            for key, item in value.items()
            if key != "content_hash"
        }
    if isinstance(value, list):
        return [_without_hashes(item) for item in value]
    return value


def canonical_json(value: Any) -> str:
    """Return stable UTF-8 JSON used for content-addressed artifacts."""
    payload = _without_hashes(_jsonable(value))
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def stamp_content_hash(model: TModel) -> TModel:
    return model.model_copy(update={"content_hash": content_hash(model)})


def verify_content_hash(model: BaseModel) -> bool:
    stored = str(getattr(model, "content_hash", "") or "")
    return bool(stored) and stored == content_hash(model)


def redact_value(value: Any) -> tuple[Any, list[str]]:
    """Redact common PII/secrets while preserving deterministic structure."""
    labels: set[str] = set()

    def visit(item: Any) -> Any:
        if isinstance(item, dict):
            return {str(key): visit(child) for key, child in item.items()}
        if isinstance(item, list):
            return [visit(child) for child in item]
        if isinstance(item, tuple):
            return [visit(child) for child in item]
        if not isinstance(item, str):
            return item
        text = item
        for label, pattern, replacement in _SECRET_PATTERNS:
            text, count = pattern.subn(replacement, text)
            if count:
                labels.add(label)
        return text

    return visit(_jsonable(value)), sorted(labels)


def find_sensitive_values(value: Any) -> list[str]:
    serialized = json.dumps(_jsonable(value), ensure_ascii=False)
    for marker in _REDACTED_MARKERS:
        serialized = serialized.replace(marker, "")
    return sorted({label for label, pattern, _ in _SECRET_PATTERNS if pattern.search(serialized)})


def write_model(path: Path, model: BaseModel, *, immutable: bool = False) -> None:
    path = path.resolve()
    if immutable and path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if canonical_json(existing) != canonical_json(model):
            raise FileExistsError(f"Frozen artifact already exists with different content: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(model.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(path)


def read_model(path: Path, model_type: type[TModel]) -> TModel:
    return model_type.model_validate_json(path.read_text(encoding="utf-8"))


def artifact_paths(case_dir: Path) -> tuple[Path, Path, Path]:
    return case_dir / "case.json", case_dir / "evidence.json", case_dir / "golden.json"


def load_artifact_set(case_dir: Path) -> tuple[EvalCase, EvidenceBundle, GoldenSpec]:
    case_path, evidence_path, golden_path = artifact_paths(case_dir)
    return (
        read_model(case_path, EvalCase),
        read_model(evidence_path, EvidenceBundle),
        read_model(golden_path, GoldenSpec),
    )


def _issue(code: str, message: str, path: str = "") -> LintIssue:
    return LintIssue(code=code, message=message, path=path)


def lint_artifact_set(
    case: EvalCase,
    evidence: EvidenceBundle,
    golden: GoldenSpec,
    *,
    require_frozen: bool = False,
) -> list[LintIssue]:
    """Apply integrity, source, citation, redaction, and temporal checks."""
    issues: list[LintIssue] = []
    if not (case.case_id == evidence.case_id == golden.case_id):
        issues.append(_issue("case_id_mismatch", "case/evidence/golden case_id values differ"))
    if not (case.version == evidence.case_version == golden.case_version):
        issues.append(_issue("case_version_mismatch", "case/evidence/golden versions differ"))
    if require_frozen and case.lifecycle.value != "frozen":
        issues.append(_issue("not_frozen", "frozen suite references a non-frozen case", "case.lifecycle"))

    for name, model in (("case", case), ("evidence", evidence), ("golden", golden)):
        if not verify_content_hash(model):
            issues.append(_issue("hash_mismatch", f"{name} content_hash is missing or invalid", name))
        sensitive = find_sensitive_values(model)
        if sensitive:
            issues.append(_issue("unredacted_sensitive_data", f"{name} contains: {', '.join(sensitive)}", name))

    if case.expected_tool != evidence.tool_name or case.expected_tool != golden.expected_tool:
        issues.append(_issue("expected_tool_mismatch", "expected tool differs across artifacts"))
    if case.as_of != evidence.as_of:
        issues.append(_issue("as_of_mismatch", "case.as_of differs from evidence.as_of"))

    nested_as_of = evidence.tool_result.get("as_of")
    if nested_as_of:
        try:
            parsed = datetime.fromisoformat(str(nested_as_of).replace("Z", "+00:00"))
        except ValueError:
            issues.append(_issue("invalid_tool_as_of", "tool_result.as_of is not ISO-8601", "evidence.tool_result.as_of"))
        else:
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                issues.append(_issue("naive_tool_as_of", "tool_result.as_of lacks a timezone", "evidence.tool_result.as_of"))
            elif parsed != evidence.as_of:
                issues.append(_issue("tool_as_of_mismatch", "tool_result.as_of differs from evidence.as_of", "evidence.tool_result.as_of"))

    if evidence.data_watermark > evidence.as_of:
        issues.append(_issue(
            "watermark_after_as_of",
            "evidence.data_watermark must not be later than evidence.as_of",
            "evidence.data_watermark",
        ))

    source_ids = [source.source_id for source in evidence.sources]
    duplicate_ids = sorted({item for item in source_ids if source_ids.count(item) > 1})
    if duplicate_ids:
        issues.append(_issue("duplicate_source_ids", f"duplicate source IDs: {duplicate_ids}", "evidence.sources"))
    known_sources = set(source_ids)
    for source in evidence.sources:
        if source.occurred_at and source.occurred_at > evidence.as_of:
            issues.append(_issue(
                "source_after_as_of",
                f"{source.source_id} occurs after the frozen as_of timestamp",
                f"evidence.sources.{source.source_id}.occurred_at",
            ))
        if source.occurred_at and source.occurred_at > evidence.data_watermark:
            issues.append(_issue(
                "future_source",
                f"{source.source_id} occurs after the data watermark",
                f"evidence.sources.{source.source_id}.occurred_at",
            ))

    cited = set(_CITATION_RE.findall(golden.golden_answer))
    unknown_citations = sorted(cited - known_sources)
    if unknown_citations:
        issues.append(_issue("unknown_golden_citation", f"unknown citations: {unknown_citations}", "golden.golden_answer"))
    if not cited:
        issues.append(_issue("golden_missing_citations", "golden answer contains no source citations", "golden.golden_answer"))

    claim_ids: set[str] = set()
    for claim in golden.required_claims:
        if claim.claim_id in claim_ids:
            issues.append(_issue("duplicate_claim_id", f"duplicate claim_id {claim.claim_id}", "golden.required_claims"))
        claim_ids.add(claim.claim_id)
        missing = sorted(set(claim.supporting_source_ids) - known_sources)
        if missing:
            issues.append(_issue("claim_unknown_source", f"{claim.claim_id} references {missing}", "golden.required_claims"))

    result_total = evidence.tool_result.get("total")
    if result_total == 0:
        summary_id = evidence.tool_result.get("summary_source_id")
        if not summary_id or summary_id not in known_sources:
            issues.append(_issue("zero_result_missing_summary", "zero-result evidence needs a real summary source"))
        if summary_id and f"[[source:{summary_id}]]" not in golden.golden_answer:
            issues.append(_issue("zero_result_uncited_summary", "zero-result golden must cite its summary source"))

    return issues


def lint_case_directory(case_dir: Path, *, require_frozen: bool = False) -> list[LintIssue]:
    try:
        artifacts = load_artifact_set(case_dir)
    except Exception as exc:
        return [_issue("artifact_load_error", f"{type(exc).__name__}: {exc}", str(case_dir))]
    return lint_artifact_set(*artifacts, require_frozen=require_frozen)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
