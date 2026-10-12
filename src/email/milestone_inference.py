"""Legacy milestone candidate hints retained for retrieval and shadow comparison.

Production inbound-email milestone decisions must use the deterministic project-state
evaluator. These regex helpers are not sufficient acceptance evidence.
"""

from __future__ import annotations

import re

from src.milestone.state_machine import MILESTONE_SEQUENCE
from src.models.enums import Milestone

# Highest milestone index wins when multiple patterns match.
_STAGE_PATTERNS: list[tuple[Milestone, re.Pattern[str]]] = [
    (
        Milestone.M8_GO_LIVE,
        re.compile(
            r"go[\s-]?live|first trade|production logon confirmed|已上線"
            r"|production launch|ready for production|生產上線",
            re.IGNORECASE,
        ),
    ),
    (
        Milestone.M7_LOG_REVIEW_AND_PRODUCTION_SESSION_DELIVERY,
        re.compile(
            r"production connectivity test|\bPCT\b|prod(?:uction)?\s+session|log review",
            re.IGNORECASE,
        ),
    ),
    (
        Milestone.M6_TEST_REPORT_AND_PRODUCTION_PREPARATION,
        re.compile(r"test report|production preparation", re.IGNORECASE),
    ),
    (
        Milestone.M5_CONFORMANCE_TEST_CASES,
        re.compile(
            r"conformance|test case|\bT0\d{2}\b|conformance test|"
            r"partial fill order|Tag 11=|conformance test results?",
            re.IGNORECASE,
        ),
    ),
    (
        Milestone.M4_APPLICATION_TESTING,
        re.compile(
            r"Demo Provisioning\s*hub|counterparty setup|hub setup complete",
            re.IGNORECASE,
        ),
    ),
    (
        Milestone.M3_UAT_ENVIRONMENT_ACCESS,
        re.compile(
            r"UAT\s*(?:login|logon|access)|logged on.*session|test order submitted|"
            r"UAT credentials",
            re.IGNORECASE,
        ),
    ),
    (
        Milestone.M2_UAT_SESSION_CONFIGURATION,
        re.compile(
            r"SenderCompID|TargetCompID|session parameters? agreed",
            re.IGNORECASE,
        ),
    ),
    (
        Milestone.M1_NETWORK_SETUP,
        re.compile(
            r"network setup|firewall.*open|VPN.*ready|connectivity.*confirmed",
            re.IGNORECASE,
        ),
    ),
]

_EXPLICIT_MILESTONE_RE = re.compile(
    r"\bM([1-8])[_\s]?(?:NetworkSetup|UATSessionConfiguration|UATEnvironmentAccess|"
    r"ApplicationTesting|ConformanceTestCases|TestReportAndProductionPreparation|"
    r"LogReviewAndProductionSessionDelivery|GoLive)?\b",
    re.IGNORECASE,
)

_COMPLETION_RE = re.compile(
    r"complete[d]?|passed|confirmed|enclosed|attached|provided|delivered|"
    r"submitted|已提供|已確認|已通過|通過|附上|附檔|excel file|new excel",
    re.IGNORECASE,
)

_REQUEST_ONLY_RE = re.compile(
    r"please provide|please send|kindly provide|requesting|awaiting|"
    r"請提供|請再提供|麻煩提供",
    re.IGNORECASE,
)

_NO_MOVEMENT_RE = re.compile(
    r"no explicit milestone|no milestone movement|milestone unchanged",
    re.IGNORECASE,
)


def _milestone_index(value: str | Milestone | None) -> int | None:
    if value is None:
        return None
    try:
        ms = value if isinstance(value, Milestone) else Milestone(value)
        return MILESTONE_SEQUENCE.index(ms)
    except (ValueError, KeyError):
        return None


def _explicit_milestone_from_text(text: str) -> Milestone | None:
    best: Milestone | None = None
    best_idx = -1
    for m in _EXPLICIT_MILESTONE_RE.finditer(text):
        token = m.group(1)
        for ms in Milestone:
            if ms.name.startswith(f"M{token}_"):
                idx = _milestone_index(ms)
                if idx is not None and idx > best_idx:
                    best_idx = idx
                    best = ms
                break
    return best


def _implied_milestone_from_keywords(*text_parts: str) -> Milestone | None:
    combined = " ".join(part for part in text_parts if part)
    if not combined.strip():
        return None
    best: Milestone | None = None
    best_idx = -1
    for ms, pattern in _STAGE_PATTERNS:
        if pattern.search(combined):
            idx = _milestone_index(ms)
            if idx is not None and idx > best_idx:
                best_idx = idx
                best = ms
    return best


def _signals_allow_advance(
    *,
    summary: str,
    suggested_action: str,
    milestone_signals: list[str] | None,
    action_requested: str,
    key_facts: list[str] | None,
) -> bool:
    signal_text = " ".join(milestone_signals or [])
    if signal_text and _NO_MOVEMENT_RE.search(signal_text):
        return False

    corpus = " ".join(
        filter(
            None,
            [summary, suggested_action, signal_text, action_requested, *(key_facts or [])],
        )
    )
    if not corpus.strip():
        return False

    if _explicit_milestone_from_text(corpus) is not None:
        return True
    if any(
        kw in corpus.lower()
        for kw in (
            "advance", "推進", "milestone complete", "tests passed",
            "uat confirmed", "setup complete", "所有測試", "測試已通過",
            "pct passed", "production launch",
        )
    ):
        return True
    if _COMPLETION_RE.search(corpus):
        return True
    if _REQUEST_ONLY_RE.search(corpus) and not _COMPLETION_RE.search(corpus):
        return False
    # Active conformance / UAT work without explicit request-only wording.
    return _implied_milestone_from_keywords(corpus) is not None


def collect_legacy_milestone_hint(
    current_milestone: str | None,
    *,
    summary: str = "",
    suggested_action: str = "",
    milestone_signals: list[str] | None = None,
    action_requested: str = "",
    key_facts: list[str] | None = None,
) -> str | None:
    """Return a legacy milestone candidate hint, never acceptance evidence."""
    if not _signals_allow_advance(
        summary=summary,
        suggested_action=suggested_action,
        milestone_signals=milestone_signals,
        action_requested=action_requested,
        key_facts=key_facts,
    ):
        return None

    corpus = " ".join(
        filter(
            None,
            [
                summary,
                suggested_action,
                " ".join(milestone_signals or []),
                action_requested,
                " ".join(key_facts or []),
            ],
        )
    )
    explicit = _explicit_milestone_from_text(corpus)
    implied = _implied_milestone_from_keywords(corpus)
    target = explicit or implied
    if target is None:
        return None

    current_idx = _milestone_index(current_milestone)
    target_idx = _milestone_index(target)
    if target_idx is None:
        return None
    if current_idx is None:
        return target.value
    if target_idx <= current_idx:
        return None
    return target.value


def backfill_proposed_fields_from_analysis(
    proposed_fields: dict,
    *,
    summary: str = "",
    suggested_action: str = "",
    body: str = "",
    current_milestone: str | None = None,
    current_next_action: str | None = None,
    confidence: float = 0.0,
    min_confidence: float = 0.85,
    allow_milestone: bool = True,
) -> dict:
    """Infer writable fields when the LLM returned summary only (no proposed_fields).

    Progress signals (PCT complete → launch prep, mid-PCT steps, etc.) always
    attempt next_action backfill — clear email evidence must not stay info_only
    solely because the LLM left proposed_fields empty.
    """
    from src.email.progress_backfill import backfill_progress_into_proposed_fields

    fields = dict(proposed_fields or {})
    summary = (summary or "").strip()
    suggested = (suggested_action or "").strip()
    # Progress-driven non-state hints run even when confidence is moderate.
    # Legacy regexes are deliberately excluded from milestone decisions.
    fields = backfill_progress_into_proposed_fields(
        fields,
        summary=summary,
        suggested_action=suggested,
        body=body,
        current_next_action=current_next_action,
        current_milestone=current_milestone,
        allow_milestone=False,
    )

    # Preserve a structured milestone candidate supplied by the evidence extractor
    # for the deterministic evaluator, but never create one here.
    if not allow_milestone:
        fields.pop("current_milestone", None)

    if confidence < min_confidence:
        return fields

    if not fields.get("next_action") and suggested:
        fields["next_action"] = suggested[:500]

    return fields