"""Privacy-safe bounded metrics and acceptance checks for the agent control plane."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import re
from threading import RLock
from typing import Any, Iterable

from src.agent.process_narrative_guard import correct_process_narrative


_EVENTS = frozenset({
    "negative_scope",
    "canonical_evidence_stop",
    "duplicate_read_replay",
    "late_read_salvage",
    "specialist_outcome",
    "specialist_checkpoint",
    "specialist_resume",
    "narrative_correction",
    "acceptance_violation",
    "tool_retry",
})
_OUTCOMES = frozenset({
    "accepted",
    "blocked_by_deadline",
    "cleared",
    "completed",
    "exhausted",
    "failed",
    "forced",
    "partial",
    "rejected",
    "replayed",
    "scheduled",
    "timed_out",
    "unverified",
})
_PHASES = frozenset({
    "initialized",
    "tool_round_complete",
    "deadline",
    "final",
    "exhausted",
    "llm_error",
    "retrieval_timeout_recovery",
    "failed",
    "finalized",
})
_CORRECTION_CODES = frozenset({
    "delegate_count_mismatch",
    "canonical_read_contradiction",
})
_ACCEPTANCE_CODES = frozenset({
    "canonical_read_reported_unread",
    "delegate_count_mismatch",
    "canonical_read_reported_complete_failure",
    "negative_scope_inherited_project",
    "duplicate_read_not_replayed",
})
_COMPLETE_FAILURE = re.compile(
    r"\b(?:complete|total|entire)\b.{0,16}\b(?:failure|failed)\b|"
    r"\b(?:no|without)\b.{0,12}(?:usable|successful|verified)?.{0,8}"
    r"\bevidence\b|"
    r"\u5b8c\u5168\u5931\u6557|\u5168\u90e8\u5931\u6557|"
    r"\u672a\u53d6\u5f97\u4efb\u4f55.{0,8}\u8b49\u64da",
    re.IGNORECASE,
)
_COUNT_PATTERNS = (
    re.compile(
        r"(?P<count>one|two|once|twice|1|2)\s+"
        r"(?:research\s+)?(?:specialist|delegat(?:ion|e))",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:research\s+)?(?:specialist|delegat(?:ion|e)).{0,32}"
        r"(?P<count>one|two|once|twice|1|2)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?P<count>\u4e00|\u5169|\u4e8c|1|2)\s*\u6b21.{0,24}"
        r"(?:specialist|\u59d4\u6d3e)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:specialist|\u59d4\u6d3e).{0,24}"
        r"(?P<count>\u4e00|\u5169|\u4e8c|1|2)\s*\u6b21",
        re.IGNORECASE,
    ),
)
_COUNT_VALUES = {
    "one": 1,
    "once": 1,
    "1": 1,
    "\u4e00": 1,
    "two": 2,
    "twice": 2,
    "2": 2,
    "\u5169": 2,
    "\u4e8c": 2,
}


def _normalized(value: object, allowed: frozenset[str]) -> str:
    text = str(value or "").strip().casefold()
    return text if text in allowed else "unknown"


class ControlPlaneMetrics:
    """Aggregation-only counters with fixed labels and a hard series cap."""

    max_series = 128

    def __init__(self) -> None:
        self._counts: Counter[str] = Counter()
        self._lock = RLock()

    def increment(self, event: str, **dimensions: object) -> None:
        if event not in _EVENTS:
            raise ValueError(f"unsupported control-plane metric: {event}")
        labels: list[str] = []
        if event == "specialist_checkpoint":
            labels.append(f"phase={_normalized(dimensions.get('phase'), _PHASES)}")
        elif event == "narrative_correction":
            labels.append(
                f"code={_normalized(dimensions.get('code'), _CORRECTION_CODES)}"
            )
        elif event == "acceptance_violation":
            labels.append(
                f"code={_normalized(dimensions.get('code'), _ACCEPTANCE_CODES)}"
            )
        else:
            labels.append(f"outcome={_normalized(dimensions.get('outcome'), _OUTCOMES)}")
        key = "|".join((event, *labels))
        with self._lock:
            if key not in self._counts and len(self._counts) >= self.max_series - 1:
                key = "series_overflow"
            self._counts[key] += 1

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(sorted(self._counts.items()))

    def reset(self) -> None:
        with self._lock:
            self._counts.clear()


@dataclass(frozen=True)
class ControlPlaneAcceptance:
    """Deterministic acceptance result derived from trusted execution facts."""

    violation_codes: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.violation_codes


def _flatten_receipts(receipts: Iterable[Any]) -> list[Any]:
    flattened: list[Any] = []
    pending = list(reversed(list(receipts)))
    while pending:
        receipt = pending.pop()
        flattened.append(receipt)
        children = getattr(receipt, "child_receipts", ()) or ()
        try:
            pending.extend(reversed(list(children)))
        except TypeError:
            continue
    return flattened


def _has_canonical_wiki_read(receipts: Iterable[Any]) -> bool:
    for receipt in _flatten_receipts(receipts):
        if (
            getattr(receipt, "tool_name", None) != "wiki_read"
            or getattr(receipt, "status", None) != "succeeded"
        ):
            continue
        for evidence in getattr(receipt, "evidence", ()) or ():
            if (
                getattr(evidence, "source_type", None) == "wiki"
                and getattr(evidence, "authority", None) == "canonical"
                and "full_document"
                in (getattr(evidence, "claim_coverage", ()) or ())
            ):
                return True
    return False


def _claimed_delegate_counts(text: str) -> set[int]:
    claims: set[int] = set()
    for pattern in _COUNT_PATTERNS:
        for match in pattern.finditer(text):
            value = _COUNT_VALUES.get(match.group("count").casefold())
            if value is not None:
                claims.add(value)
    return claims


def evaluate_control_plane_acceptance(
    candidate: str,
    *,
    receipts: Iterable[Any] = (),
    negative_scope_requested: bool = False,
    effective_project_id: str | None = None,
    scope_source: str | None = None,
    duplicate_identical_reads: int = 0,
    duplicate_read_replays: int = 0,
) -> ControlPlaneAcceptance:
    """Detect contradictions that must never pass the final serving boundary."""
    text = str(candidate or "")
    top_level = list(receipts)
    codes: list[str] = []
    narrative_codes = set(
        correct_process_narrative(text, receipts=top_level).correction_codes
    )
    canonical_read = _has_canonical_wiki_read(top_level)
    if "canonical_read_contradiction" in narrative_codes:
        codes.append("canonical_read_reported_unread")

    observed_delegate_count = sum(
        getattr(receipt, "tool_name", None) == "delegate"
        for receipt in top_level
    )
    claimed_counts = _claimed_delegate_counts(text)
    if claimed_counts and observed_delegate_count not in claimed_counts:
        codes.append("delegate_count_mismatch")

    if canonical_read and _COMPLETE_FAILURE.search(text):
        codes.append("canonical_read_reported_complete_failure")
    if negative_scope_requested and (
        effective_project_id is not None or scope_source == "active"
    ):
        codes.append("negative_scope_inherited_project")
    if max(0, int(duplicate_identical_reads)) > max(
        0, int(duplicate_read_replays)
    ):
        codes.append("duplicate_read_not_replayed")
    return ControlPlaneAcceptance(tuple(dict.fromkeys(codes)))


control_plane_metrics = ControlPlaneMetrics()
