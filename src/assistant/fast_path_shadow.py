"""Shadow-only classifier for deterministic AI Workspace fast-path opportunities."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

FAST_PATH_SHADOW_CLASSIFIER_VERSION = "workspace-fast-path-shadow-v1"

_GENERATIVE_SYNTHESIS_RE = re.compile(
    r"\b(?:analy[sz]e|analysis|compare|contrast|draft|explain|recommend|suggest|"
    r"summari[sz]e|synthesis|write|why)\b|"
    r"\bwhat should (?:i|we)\b|\bhow should (?:i|we)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class FastPathShadowDecision:
    """Observable classification result that never changes runtime routing."""

    eligible: bool
    reason: str
    tool_name: str | None
    classifier_version: str = FAST_PATH_SHADOW_CLASSIFIER_VERSION

    def as_metadata(self) -> dict[str, object]:
        return {
            "eligible": self.eligible,
            "reason": self.reason,
            "tool_name": self.tool_name,
            "classifier_version": self.classifier_version,
        }


def classify_fast_path_shadow(
    message: str,
    *,
    required_tools: Iterable[str],
    scope_mode: str,
    project_count: int,
    web_enabled: bool,
) -> FastPathShadowDecision:
    """Classify a possible deterministic read path without executing or routing it."""
    tools = tuple(sorted({str(name).strip() for name in required_tools if str(name).strip()}))
    tool_name = tools[0] if len(tools) == 1 else None
    if web_enabled:
        return FastPathShadowDecision(False, "web_enabled", tool_name)
    if not tools:
        return FastPathShadowDecision(False, "no_deterministic_tool", None)
    if len(tools) > 1:
        return FastPathShadowDecision(False, "multiple_deterministic_tools", None)
    if _GENERATIVE_SYNTHESIS_RE.search(message or ""):
        return FastPathShadowDecision(False, "generative_synthesis_requested", tool_name)
    return FastPathShadowDecision(True, "eligible_single_deterministic_read", tool_name)
