"""Deterministic extraction of policy predicates from frozen email evidence."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Iterable

from src.models.progress_assessment import (
    EvidenceCitation,
    EvidenceLevel,
    EvidenceModality,
    EvidencePolarity,
    EvidenceSourceType,
    ExtractedProgressFact,
)
from src.project_reconciliation.contracts import EvidenceRecord, FrozenEvidenceBundle


_ORDER_ID = re.compile(r"\b[A-Z]{2,}[A-Z0-9]*\d{6}[_-]\d+\b")
_SESSION_DETAIL = re.compile(
    r"(?:Host|IP)\s*[:=]?\s*\d{1,3}(?:\.\d{1,3}){3}.{0,240}"
    r"(?:Port(?:\s+number)?\s*[:=]?\s*\d{2,5}).{0,240}"
    r"(?:Sender(?:Comp)?ID|SenderID).{0,120}(?:Target(?:Comp)?ID|TargetID)",
    re.IGNORECASE | re.DOTALL,
)
_NETWORK_APPLIED = re.compile(
    r"(?:VPN|whitelist|white\s*list|allowlist|NAT|firewall|放行).{0,180}"
    r"(?:completed|complete|done|active|applied|established|已放行|已完成|完成)",
    re.IGNORECASE | re.DOTALL,
)
_CONNECTIVITY_FAILED = re.compile(
    r"(?:telnet|connect(?:ion)?).{0,140}"
    r"(?:failed|fail|cannot|can't|unable|timeout|不到|連不上)",
    re.IGNORECASE | re.DOTALL,
)
_CANCEL_DONE = re.compile(
    r"(?:(?:unsolicited\s+cancel|cancel).{0,180}"
    r"(?:completed|complete|done|cancelled|canceled|已完成|完成)|"
    r"(?:completed|complete|done|已完成|完成).{0,100}(?:unsolicited\s+cancel|cancel))",
    re.IGNORECASE | re.DOTALL,
)
_APPLICATION_SETUP_DONE = re.compile(
    r"(?:Demo Provisioning|counterparty|account\s+mapping|routing).{0,160}"
    r"(?:setup|configuration|mapping).{0,100}"
    r"(?:completed|complete|done|ready|已完成|完成|就緒)",
    re.IGNORECASE | re.DOTALL,
)
_CONFORMANCE_READY = re.compile(
    r"(?:ready\s+for\s+conformance|conformance.{0,80}(?:ready|can\s+start)|"
    r"可開始.{0,30}conformance|開始.{0,30}conformance)",
    re.IGNORECASE | re.DOTALL,
)
_GO_LIVE_DONE = re.compile(
    r"(?:go[- ]?live|live\s+in\s+production).{0,120}"
    r"(?:completed|successful|started|已上線|成功|完成)",
    re.IGNORECASE | re.DOTALL,
)


def _event_at(value: str) -> datetime | None:
    if not value:
        return None
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _actor(record: EvidenceRecord) -> str | None:
    # Message text and person-name substrings cannot grant support authority.
    return record.sender or None


def _citation(
    record: EvidenceRecord,
    quote: str,
    *,
    level: EvidenceLevel,
    polarity: EvidencePolarity = EvidencePolarity.POSITIVE,
) -> EvidenceCitation:
    return EvidenceCitation(
        source_type=EvidenceSourceType.EMAIL,
        source_ref=record.source_ref,
        quote=quote.strip(),
        actor=_actor(record),
        event_at=_event_at(record.event_at),
        evidence_level=level,
        polarity=polarity,
        modality=EvidenceModality.CONFIRMED,
    )


def _fact(
    predicate: str,
    citation: EvidenceCitation,
    *,
    value: str | bool = True,
    confidence: float = 0.96,
) -> ExtractedProgressFact:
    return ExtractedProgressFact(
        predicate=predicate,
        value=value,
        citations=[citation],
        extraction_confidence=confidence,
    )


def _matches(
    records: Iterable[EvidenceRecord], regex: re.Pattern[str],
) -> list[tuple[EvidenceRecord, str]]:
    found: list[tuple[EvidenceRecord, str]] = []
    for record in records:
        match = regex.search(record.content)
        if match:
            found.append((record, match.group(0)))
    return found


def extract_policy_facts(bundle: FrozenEvidenceBundle, *, source_authorities=None, delivery_confirmations=None) -> list[ExtractedProgressFact]:
    """Extract a conservative fact set consumed only by the contract evaluator."""
    records = list(bundle.records)
    facts: list[ExtractedProgressFact] = []

    if records:
        citation = _citation(
            records[0],
            records[0].subject or records[0].content[:120],
            level=EvidenceLevel.L3_NAMED_AUTHORITY_WRITTEN,
        )
        facts.append(_fact("onboarding_project_identified", citation))

    for record, quote in _matches(records, _SESSION_DETAIL):
        l2 = _citation(record, quote, level=EvidenceLevel.L2_AUTHORITATIVE_SYSTEM)
        l3 = _citation(record, quote, level=EvidenceLevel.L3_NAMED_AUTHORITY_WRITTEN)
        facts.extend([
            _fact("uat_endpoint_and_port_configured", l2),
            _fact("uat_session_created", l3),
            _fact("compids_assigned", l3),
            _fact("uat_session_endpoint_confirmed", l3),
            _fact("session_parameters_confirmed", l3),
        ])

    for record, quote in _matches(records, _NETWORK_APPLIED):
        citation = _citation(record, quote, level=EvidenceLevel.L2_AUTHORITATIVE_SYSTEM)
        facts.extend([
            _fact("ip_allowlist_applied", citation),
            _fact("uat_network_path_configured", citation),
        ])

    for record, quote in _matches(records, _CONNECTIVITY_FAILED):
        citation = _citation(
            record,
            quote,
            level=EvidenceLevel.L2_AUTHORITATIVE_SYSTEM,
            polarity=EvidencePolarity.NEGATIVE,
        )
        facts.append(_fact("tcp_connectivity_test_passed", citation, value=False))

    from src.email.body_cleaner import clean_email_body
    non_completion = re.compile(r"\b(?:will|would|could|may|might|plan(?:ned)?|if|not|never|failed|unable)\b", re.I)
    for record in records:
        authored = clean_email_body(record.content)
        # Observed processing of an identified test order demonstrates application
        # testing, not a separately confirmed IP/port test, logon, or passing ER.
        for line in authored.splitlines():
            if not _ORDER_ID.search(line) or not _CANCEL_DONE.search(line):
                continue
            if non_completion.search(line):
                continue
            citation = _citation(record, line, level=EvidenceLevel.L4_TRUSTED_PARTY_WRITTEN)
            facts.append(_fact("application_testing_started", citation))

    for record, quote in _matches(records, _APPLICATION_SETUP_DONE):
        citation = _citation(record, quote, level=EvidenceLevel.L2_AUTHORITATIVE_SYSTEM)
        facts.extend([
            _fact("application_testing_setup_complete", citation),
            _fact("application_routing_configuration_complete", citation),
        ])
    for record, quote in _matches(records, _CONFORMANCE_READY):
        citation = _citation(record, quote, level=EvidenceLevel.L3_NAMED_AUTHORITY_WRITTEN)
        facts.append(_fact("authorized_ready_for_conformance_confirmation", citation))
    from src.project_reconciliation.progress_facts import extract_progress_facts
    facts.extend(extract_progress_facts(bundle, source_authorities or {}, delivery_confirmations=delivery_confirmations))
    for record, quote in _matches(records, _GO_LIVE_DONE):
        citation = _citation(record, quote, level=EvidenceLevel.L2_AUTHORITATIVE_SYSTEM)
        facts.append(_fact("first_required_live_activity_observed", citation))

    deduped: dict[tuple[str, str, str], ExtractedProgressFact] = {}
    for fact in facts:
        citation = fact.citations[0]
        key = (fact.predicate, citation.source_ref, citation.quote)
        deduped[key] = fact
    return list(deduped.values())
