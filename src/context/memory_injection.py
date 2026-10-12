"""Single rollout adapter for governed memory across every context surface."""
from __future__ import annotations

import hashlib
import logging
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from src.memory.exposure_repo import ExposureCandidate

from src.memory.renderer import budget_for_profile, render_memory
from src.memory.retriever import MemoryRetrievalRequest, MemoryRetriever

logger = logging.getLogger(__name__)


class MemorySurface(str, Enum):
    CHAT = "chat"
    WEB = "web"
    DRAFT = "draft"
    ADVISORY = "advisory"
    TOOL = "tool"


class MemoryShadowDiff(BaseModel):
    model_config = ConfigDict(frozen=True)
    surface: MemorySurface
    changed: bool
    legacy_hash: str
    v2_hash: str
    legacy_chars: int
    v2_chars: int


class MemoryInjectionResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    text: str
    used_v2: bool
    suppress_legacy_authority: bool = False
    diagnostics: list[str] = []
    shadow_diff: MemoryShadowDiff | None = None
    exposure_candidates: list[ExposureCandidate] = Field(default_factory=list)


class MemoryInjectionAdapter:
    def __init__(self, retriever: MemoryRetriever | None = None):
        self._retriever = retriever or MemoryRetriever()

    def inject(self, *, legacy_text: str, request: MemoryRetrievalRequest,
               surface: MemorySurface, v2_enabled: bool, shadow_enabled: bool,
               retrieval_disabled: bool, telemetry_conn=None, cohort: str = "default",
               policy_version: int | None = None, trace_id: str | None = None,
               output_revision_id: str | None = None) -> MemoryInjectionResult:
        """Return legacy unless the per-surface gate is on; shadow never changes output."""
        if retrieval_disabled:
            return MemoryInjectionResult(text=legacy_text, used_v2=False)
        if not v2_enabled and not shadow_enabled:
            return MemoryInjectionResult(text=legacy_text, used_v2=False)
        if not request.operator_id:
            return MemoryInjectionResult(text=legacy_text, used_v2=False)
        try:
            retrieved = self._retriever.retrieve(request)
            if "missing_operator_authorization" in retrieved.diagnostics:
                return MemoryInjectionResult(
                    text=legacy_text, used_v2=False, diagnostics=retrieved.diagnostics,
                )
            rendered = render_memory(retrieved, budget_for_profile(request.profile))
            v2_text = rendered.text
        except Exception:
            logger.warning("governed memory retrieval failed; retaining legacy", exc_info=True)
            if shadow_enabled and telemetry_conn is not None:
                _persist_shadow(telemetry_conn, surface, request, cohort, policy_version,
                                legacy_text, "", [], trace_id, output_revision_id,
                                "retrieval_error")
            return MemoryInjectionResult(
                text=legacy_text, used_v2=False, diagnostics=["retrieval_error"],
            )
        diff = _shadow_diff(surface, legacy_text, v2_text) if shadow_enabled else None
        if diff is not None:
            logger.info("memory_retrieval_shadow %s", diff.model_dump_json())
            if telemetry_conn is not None:
                _persist_shadow(
                    telemetry_conn, surface, request, cohort, policy_version,
                    legacy_text, v2_text,
                    [(item.fact_id, item.fact_version) for item in rendered.exposure_candidates],
                    trace_id, output_revision_id, None,
                )
        active = v2_enabled and not shadow_enabled
        usable_v2 = active and bool(v2_text.strip())
        return MemoryInjectionResult(
            text=v2_text if usable_v2 else legacy_text,
            used_v2=usable_v2,
            suppress_legacy_authority=usable_v2,
            diagnostics=retrieved.diagnostics,
            shadow_diff=diff,
            exposure_candidates=rendered.exposure_candidates if usable_v2 else [],
        )


def _shadow_diff(surface: MemorySurface, legacy: str, v2: str) -> MemoryShadowDiff:
    digest = lambda text: hashlib.sha256(text.encode("utf-8")).hexdigest()
    return MemoryShadowDiff(surface=surface, changed=legacy != v2,
                            legacy_hash=digest(legacy), v2_hash=digest(v2),
                            legacy_chars=len(legacy), v2_chars=len(v2))


def _persist_shadow(conn, surface, request, cohort, policy_version, legacy_text, v2_text,
                    fact_versions, trace_id, output_revision_id, error_category):
    from src.learning.shadow_parity import ShadowParityRecord, record_shadow_parity_fail_open
    record_shadow_parity_fail_open(ShadowParityRecord(
        surface=surface.value, operator_id=request.operator_id, project_id=request.project_id,
        cohort=cohort, policy_version=policy_version, legacy_text=legacy_text,
        v2_text=v2_text, fact_versions=fact_versions, trace_id=trace_id,
        output_revision_id=output_revision_id, error_category=error_category,
    ))
