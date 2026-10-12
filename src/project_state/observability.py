"""Privacy-safe bounded in-process counters for project-state rollout."""

from __future__ import annotations

from collections import Counter
from functools import lru_cache
from threading import RLock

_FIELDS = frozenset({"current_milestone", "project_status", "ball_with", "next_action"})
_REVIEW_OUTCOMES = frozenset({"approved", "rejected", "edited"})
_CODES = frozenset({
    "INSUFFICIENT_EXIT_EVIDENCE", "CONTRADICTORY_EVIDENCE",
    "AMBIGUOUS_PROJECT_IDENTITY", "FUTURE_INTENT_NOT_COMPLETION",
    "CONDITIONAL_STATEMENT", "PARTIAL_COMPLETION", "UNAUTHORIZED_SOURCE",
    "INVALID_STATE_TRANSITION", "TERMINAL_STATUS_REVIEW_REQUIRED",
    "NON_STANDARD_PATH_REVIEW_REQUIRED", "ASSESSMENT_MISSING",
    "ASSESSMENT_PARSE_FAILURE", "CONTRACT_FAILURE", "SOURCE_CONTENT_UNAVAILABLE",
})
_EVENTS = frozenset({
    "candidate_proposal", "abstention", "missing_criterion", "contradiction",
    "review", "legacy_new_disagreement", "stale_proposal_rejection",
    "assessment_parse_failure", "contract_failure",
})
_MILESTONES = frozenset({"none", *(f"M{i}" for i in range(1, 9))})


def _field(value: object) -> str:
    text = str(value)
    return text if text in _FIELDS else "unknown"


def _code(value: object) -> str:
    text = getattr(value, "value", value)
    return str(text) if str(text) in _CODES else "UNKNOWN"


@lru_cache(maxsize=1)
def _criterion_ids() -> frozenset[str]:
    try:
        from src.milestone.contracts import load_milestone_contract_policy

        policy = load_milestone_contract_policy()
        return frozenset(
            criterion.id
            for milestone in policy.milestones
            for group in (
                milestone.entry_criteria,
                milestone.required_exit_criteria,
                milestone.optional_exit_criteria,
                milestone.close_criteria,
            )
            for criterion in group
        )
    except Exception:
        return frozenset()


def _criterion(value: object) -> str:
    text = str(value)
    return text if text in _criterion_ids() else "unknown"


def _milestone(value: object) -> str:
    text = str(value or "none")
    prefix = text.split("_", 1)[0]
    return prefix if prefix in _MILESTONES else "unknown"


class ProjectStateMetrics:
    """Aggregation-only counters with normalized labels and a hard series cap."""

    max_series = 256

    def __init__(self) -> None:
        self._counts: Counter[str] = Counter()
        self._lock = RLock()

    def increment(self, event: str, **dimensions: object) -> None:
        if event not in _EVENTS:
            raise ValueError(f"unsupported project-state metric: {event}")
        labels: list[str] = []
        if event == "candidate_proposal":
            field = _field(dimensions.get("field"))
            labels.append(f"field={field}")
            if field == "current_milestone":
                raw = str(dimensions.get("transition", "none->none"))
                old, _, new = raw.partition("->")
                labels.append(f"transition={_milestone(old)}->{_milestone(new)}")
        elif event == "abstention":
            labels.append(f"code={_code(dimensions.get('code'))}")
        elif event == "missing_criterion":
            labels.append(f"criterion={_criterion(dimensions.get('criterion'))}")
        elif event == "contradiction":
            labels.append(f"field={_field(dimensions.get('field'))}")
        elif event == "review":
            outcome = str(dimensions.get("outcome"))
            labels.append(f"outcome={outcome if outcome in _REVIEW_OUTCOMES else 'unknown'}")
        elif event == "legacy_new_disagreement":
            labels.extend((f"legacy={_milestone(dimensions.get('legacy'))}", f"new={_milestone(dimensions.get('new'))}"))
        key = "|".join((event, *labels))
        with self._lock:
            if (
                key not in self._counts
                and len(self._counts) >= self.max_series - 1
            ):
                key = "series_overflow"
            self._counts[key] += 1

    def record_candidate(self, field: object, current: object, candidate: object) -> None:
        transition = f"{current}->{candidate}" if str(field) == "current_milestone" else None
        self.increment("candidate_proposal", field=field, transition=transition)

    def record_abstention(self, code: object) -> None:
        self.increment("abstention", code=code)

    def record_missing_criterion(self, criterion: object) -> None:
        self.increment("missing_criterion", criterion=criterion)

    def record_contradiction(self, field: object) -> None:
        self.increment("contradiction", field=field)

    def record_review(self, outcome: object) -> None:
        self.increment("review", outcome=outcome)

    def record_disagreement(self, legacy: object, new: object) -> None:
        self.increment("legacy_new_disagreement", legacy=legacy, new=new)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(sorted(self._counts.items()))

    def reset(self) -> None:
        with self._lock:
            self._counts.clear()


project_state_metrics = ProjectStateMetrics()
