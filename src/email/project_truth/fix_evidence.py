"""Deterministic validation and enrichment for bounded FIX transcript evidence."""

from __future__ import annotations

import re

from src.email.project_truth.contracts import EmailProjectTruthCase
from src.models.progress_assessment import (
    EvidenceLevel,
    EvidenceModality,
    EvidencePolarity,
    EvidenceCitation,
    ExtractedProgressFact,
)

_VALIDATED_SESSION_PREDICATE = "validated_bidirectional_fix_session_log"
_FIX_MESSAGE_PATTERN = re.compile(
    r"8=FIX\.[0-9.]+(?:\x01[^\r\n]*?)?10=\d{3}\x01"
)


def _parse_valid_fix_message(raw: str) -> dict[str, str] | None:
    """Return parsed fields only when BodyLength and CheckSum are both valid."""
    if not raw.startswith("8=FIX.") or not raw.endswith("\x01"):
        return None
    fields: dict[str, str] = {}
    for item in raw[:-1].split("\x01"):
        if "=" not in item:
            return None
        tag, value = item.split("=", 1)
        if not tag.isdigit() or tag in fields:
            return None
        fields[tag] = value
    if not {"8", "9", "10", "35", "49", "56"} <= fields.keys():
        return None
    try:
        expected_body_length = int(fields["9"])
        expected_checksum = int(fields["10"])
    except ValueError:
        return None

    body_start = raw.find("\x01", raw.find("\x019=") + 1) + 1
    checksum_start = raw.rfind("10=")
    if body_start <= 0 or checksum_start <= body_start:
        return None
    try:
        actual_body_length = len(raw[body_start:checksum_start].encode("ascii"))
        actual_checksum = sum(raw[:checksum_start].encode("ascii")) % 256
    except UnicodeEncodeError:
        return None
    if actual_body_length != expected_body_length or actual_checksum != expected_checksum:
        return None
    return fields


def find_valid_bidirectional_heartbeat_exchange(text: str) -> tuple[str, str] | None:
    """Return a reciprocal pair of complete, valid FIX heartbeat messages."""
    messages = [
        (match.group(0), parsed)
        for match in _FIX_MESSAGE_PATTERN.finditer(text)
        if (parsed := _parse_valid_fix_message(match.group(0))) is not None
        and parsed.get("35") == "0"
    ]
    for left_raw, left in messages:
        for right_raw, right in messages:
            if left is right:
                continue
            if left["49"] == right["56"] and left["56"] == right["49"]:
                return left_raw, right_raw
    return None


def has_valid_bidirectional_heartbeat_exchange(text: str) -> bool:
    """Validate a reciprocal pair of complete FIX heartbeat messages."""
    return find_valid_bidirectional_heartbeat_exchange(text) is not None


def enrich_validated_fix_session_facts(
    case: EmailProjectTruthCase,
    facts: list[ExtractedProgressFact],
) -> list[ExtractedProgressFact]:
    """Derive L2 FIX-session evidence directly from bounded eligible sources.

    ``validated_bidirectional_fix_session_log`` is system-owned. Any extractor-emitted
    fact using that predicate is discarded, because an LLM cannot self-assign L2
    authority. The deterministic validator scans each eligible bounded source,
    validates complete reciprocal heartbeat messages, and emits exact FIX-message
    citations with an L2 evidence level.
    """
    enriched = [
        fact for fact in facts
        if fact.predicate != _VALIDATED_SESSION_PREDICATE
    ]
    for source in case.sources:
        if not source.eligible_current_evidence:
            continue
        pair = find_valid_bidirectional_heartbeat_exchange(source.text)
        if pair is None:
            continue
        citations = [
            EvidenceCitation(
                source_type=source.source_type,
                source_ref=source.source_ref,
                quote=raw_message,
                actor=source.actor,
                event_at=source.event_at,
                evidence_level=EvidenceLevel.L2_AUTHORITATIVE_SYSTEM,
                polarity=EvidencePolarity.POSITIVE,
                modality=EvidenceModality.CONFIRMED,
            )
            for raw_message in pair
        ]
        enriched.append(ExtractedProgressFact(
            predicate=_VALIDATED_SESSION_PREDICATE,
            value="A reciprocal FIX heartbeat exchange passed BodyLength and CheckSum validation.",
            citations=citations,
            extraction_confidence=1.0,
        ))
    return enriched
