"""Bound historical progress to current predicates, authored text and source roles.

These patterns extract claims, not milestone decisions. The contract evaluator
and operator review remain authoritative; PCT/go-live completion is not inferred.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime, timezone

from src.email.body_cleaner import clean_email_body
from src.email.source_authority import SourceAuthority, resolve_source_authority
from src.models.progress_assessment import (
    EvidenceCitation, EvidenceLevel, EvidenceModality, EvidencePolarity, EvidenceSourceType,
    ExtractedProgressFact,
)
from src.project_reconciliation.contracts import FrozenEvidenceBundle
from src.project_reconciliation.delivery_confirmation import clarified_identity_text

CANONICAL_PROGRESS_PREDICATES = frozenset({
    "formal_conformance_testing_started",
    "test_report_all_mandatory_cases_passed",
    "provisioning_support_production_session_information_delivered",
})
_UNCONFIRMED = re.compile(
    r"\b(?:not|never|cannot|unable|failed|will|would|could|may|might|"
    r"plan(?:ned|ning)?|expect(?:ed)?|if|unless|when|once|whether|should|must|please|can you|"
    r"says?|reportedly)\b|[?\uff1f]", re.I,
)
_CONFORMANCE = re.compile(
    r"\bconformance\b.{0,160}\b(?:started|in progress|executing|"
    r"completed|complete|passed)\b|"
    r"\b(?:started|executing|completed)\b.{0,50}\bconformance\b", re.I,
)
_REPORT = re.compile(
    r"\b(?:test|conformance) report\b", re.I,
)
_SUBMITTED = re.compile(r"\b(?:submitted|attached|provided|received)\b", re.I)
_ALL_PASSED = re.compile(
    r"\ball (?:mandatory|required) (?:test )?cases\b.{0,40}\bpassed\b", re.I,
)
_PRODUCTION = re.compile(
    r"(?:for production FIX connection|production session (?:information|details)|"
    r"production (?:session|connection) (?:settings|configuration))", re.I,
)
_COMPID = {
    name: re.compile(rf"\b{name}(?:Comp)?ID\s*[:=]\s*[A-Z0-9_.-]+", re.I)
    for name in ("Sender", "Target")
}
_ENDPOINT = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b.{0,60}\bport\s*[:=]?\s*\d{2,5}", re.I)

_NEGATIVE_CLAIMS = {
    "formal_conformance_testing_started": re.compile(
        r"\bconformance\b.{0,80}\b(?:not|never)\s+(?:(?:yet|been)\s+)*started\b", re.I,
    ),
    "test_report_all_mandatory_cases_passed": re.compile(
        r"\b(?:test|conformance) report\b.{0,120}\b(?:mandatory|required)\b.{0,60}"
        r"\b(?:failed|incomplete|not passed)\b", re.I,
    ),
    "provisioning_support_production_session_information_delivered": re.compile(
        r"\bproduction session\b.{0,80}\b(?:not|never)\s+(?:(?:yet|been)\s+)*delivered\b", re.I,
    ),
}
_NONASSERTED = re.compile(r"\b(?:if|unless|will|would|could|may|might|please|says?|reportedly)\b|[?\uff1f]", re.I)


def resolve_bundle_authorities(conn, bundle: FrozenEvidenceBundle) -> dict[str, SourceAuthority]:
    """Bind to current database roles, never body names or regex-assigned L2."""
    return {
        record.source_ref: resolve_source_authority(
            conn, project_id=bundle.project_id, sender_email=record.sender,
        )
        for record in bundle.records
    }


def _foreign_identity(text: str, project_id: str) -> bool:
    # Explicit project codes and FIX sender IDs cannot silently cross projects.
    expected = project_id.upper()
    short = re.sub(r"\d{4}$", "", expected)
    codes = re.findall(r"\[([A-Z]{2,}\d{4})\]", text)
    codes += re.findall(r"\b([A-Z]{2,})FIX[TP]\d+[A-Z]?\b", text)
    codes += re.findall(r"(?i:\b(?:for|of|project))\s+([A-Z]{2,}(?:\d{4})?)\b", text)
    codes = [code for code in codes if code not in {"HK", "UAT", "FIX", "PCT", "PM"}]
    return any(code not in {expected, short} for code in codes)


def extract_progress_facts(
    bundle: FrozenEvidenceBundle,
    authorities: Mapping[str, SourceAuthority],
    *, delivery_confirmations=None,
) -> list[ExtractedProgressFact]:
    facts = []
    for record in bundle.records:
        confirmation_entry = (delivery_confirmations or {}).get(record.source_ref)
        authority = authorities.get(record.source_ref)
        if authority is None:
            authority = SourceAuthority(record.sender, EvidenceLevel.L5_UNVERIFIED_OR_INDIRECT, "unknown")
        if authority.evidence_level == EvidenceLevel.L5_UNVERIFIED_OR_INDIRECT and not confirmation_entry:
            continue
        try:
            event_at = datetime.fromisoformat(record.event_at.replace("Z", "+00:00"))
            if event_at.tzinfo is None:
                event_at = event_at.replace(tzinfo=timezone.utc)
            frozen_at = datetime.fromisoformat(bundle.frozen_at.replace("Z", "+00:00"))
            if event_at > frozen_at.replace(tzinfo=frozen_at.tzinfo or timezone.utc):
                continue
        except (ValueError, TypeError):
            continue
        body = record.content
        if body.startswith("Subject:"):
            body = body.partition("\n")[2]
        authored = clean_email_body(body)
        lines = [line.strip() for line in authored.splitlines() if line.strip()]

        def add(predicate: str, quote: str, *, positive: bool = True, binding=None, confirmation_id=None) -> None:
            if not quote or quote not in record.content:
                return
            bound = binding or authority
            facts.append(ExtractedProgressFact(
                predicate=predicate, value=positive, extraction_confidence=0.96,
                subject=f"delivery-confirmation:{confirmation_id}" if confirmation_id else None,
                citations=[EvidenceCitation(
                    source_type=EvidenceSourceType.EMAIL, source_ref=record.source_ref,
                    quote=quote, actor=bound.actor, event_at=event_at,
                    evidence_level=bound.evidence_level, modality=EvidenceModality.CONFIRMED,
                    polarity=EvidencePolarity.POSITIVE if positive else EvidencePolarity.NEGATIVE,
                )],
            ))

        for line in lines:
            if _foreign_identity(line, bundle.project_id):
                continue
            if not _NONASSERTED.search(line):
                for predicate, pattern in _NEGATIVE_CLAIMS.items():
                    if pattern.search(line):
                        add(predicate, line, positive=False)
            if _UNCONFIRMED.search(line):
                continue
            if _CONFORMANCE.search(line):
                # Completion proves execution occurred, not report submission.
                add("formal_conformance_testing_started", line)
            if _REPORT.search(line) and _SUBMITTED.search(line) and _ALL_PASSED.search(line):
                add("test_report_all_mandatory_cases_passed", line)

        # Concrete settings are delivery evidence, not a request or a PCT pass.
        headings = [line for line in lines if _PRODUCTION.search(line) and not _UNCONFIRMED.search(line)]
        identity_text = authored
        delivery_authority = authority
        confirmation_id = None
        if confirmation_entry:
            confirmation_id, confirmation = confirmation_entry
            if confirmation.project_id != bundle.project_id:
                continue
            try:
                identity_text = clarified_identity_text(record, confirmation)
            except ValueError:
                continue
            delivery_authority = SourceAuthority(confirmation.authority,
                EvidenceLevel.L3_NAMED_AUTHORITY_WRITTEN, "operator_confirmed_project_email_delivery")
        if not headings or _foreign_identity(identity_text, bundle.project_id):
            continue
        details = [line for line in lines if _ENDPOINT.search(line) or any(p.search(line) for p in _COMPID.values())]
        detail_text = "\n".join(details)
        if not _ENDPOINT.search(detail_text) or not all(p.search(detail_text) for p in _COMPID.values()):
            continue
        if any(_UNCONFIRMED.search(line) for line in details):
            continue
        selected = headings + details
        if any(line not in record.content for line in selected):
            continue
        start = min(record.content.index(line) for line in selected)
        end = max(record.content.index(line) + len(line) for line in selected)
        add("provisioning_support_production_session_information_delivered", record.content[start:end],
            binding=delivery_authority, confirmation_id=confirmation_id)
    return facts
