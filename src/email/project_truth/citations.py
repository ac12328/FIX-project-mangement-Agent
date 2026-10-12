"""Exact, bounded citation validation for Email -> Project Truth."""

from __future__ import annotations

import re
from src.models.progress_assessment import EvidenceSourceType
import unicodedata

from src.email.project_truth.contracts import (
    CitationDiagnostic,
    EmailFactExtraction,
    EmailProjectTruthCase,
    ValidatedEmailEvidence,
)
from src.models.progress_assessment import EvidenceCitation, EvidenceModality, ExtractedProgressFact


def normalize_exact_text(value: str) -> str:
    """Apply only the normalization allowed by the research protocol."""
    return unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))


def _normalized_text_with_source_spans(value: str) -> tuple[str, list[tuple[int, int]]]:
    """Normalize text while retaining the original span for each output character."""
    normalized_parts: list[str] = []
    source_spans: list[tuple[int, int]] = []
    index = 0
    while index < len(value):
        start = index
        if value[index] == "\r":
            index += 2 if index + 1 < len(value) and value[index + 1] == "\n" else 1
            part = "\n"
        else:
            index += 1
            while index < len(value) and unicodedata.combining(value[index]):
                index += 1
            part = unicodedata.normalize("NFC", value[start:index])
        normalized_parts.append(part)
        source_spans.extend((start, index) for _ in part)
    return "".join(normalized_parts), source_spans


def recover_exact_quote(source_text: str, quote: str) -> str | None:
    """Recover one verbatim source span when only whitespace runs differ."""
    normalized_source, source_spans = _normalized_text_with_source_spans(source_text)
    normalized_quote = normalize_exact_text(quote).strip()
    if not normalized_quote:
        return None
    pieces = re.split(r"\s+", normalized_quote)
    pattern = r"\s+".join(re.escape(piece) for piece in pieces if piece)
    matches = list(re.finditer(pattern, normalized_source))
    if len(matches) != 1:
        return None
    match = matches[0]
    source_start = source_spans[match.start()][0]
    source_end = source_spans[match.end() - 1][1]
    return source_text[source_start:source_end]


def validate_extraction_citations(
    case: EmailProjectTruthCase,
    extraction: EmailFactExtraction,
) -> ValidatedEmailEvidence:
    """Drop invalid citations while preserving their parent facts for fail-closed evaluation."""
    source_by_ref = {source.source_ref: source for source in case.sources}
    diagnostics: list[CitationDiagnostic] = []
    accepted_facts: list[ExtractedProgressFact] = []
    total = 0
    accepted = 0

    for fact_index, fact in enumerate(extraction.facts):
        bounded: list[EvidenceCitation] = []
        for citation_index, citation in enumerate(fact.citations):
            total += 1
            source = source_by_ref.get(citation.source_ref)
            if source is None:
                quote = normalize_exact_text(citation.quote)
                matching_sources = [
                    candidate
                    for candidate in case.sources
                    if candidate.source_type == citation.source_type
                    and quote in normalize_exact_text(candidate.text)
                ]
                repairable_message_id = bool(
                    citation.source_ref.startswith("<")
                    and citation.source_ref.endswith(">")
                    and len(matching_sources) == 1
                    and matching_sources[0].source_ref.startswith("<")
                    and matching_sources[0].source_ref.endswith(">")
                )
                if repairable_message_id:
                    source = matching_sources[0]
                    diagnostics.append(CitationDiagnostic(
                        fact_index=fact_index,
                        citation_index=citation_index,
                        source_ref=citation.source_ref,
                        code="source_ref_repaired",
                        detail=(
                            "Unknown source_ref was rebound to the only bounded source "
                            "containing the exact quote and matching source_type."
                        ),
                    ))
                else:
                    diagnostics.append(CitationDiagnostic(
                        fact_index=fact_index,
                        citation_index=citation_index,
                        source_ref=citation.source_ref,
                        code="unknown_source",
                        detail="Citation source_ref is not part of the bounded case evidence.",
                    ))
                    continue
            if source.source_type != citation.source_type:
                diagnostics.append(CitationDiagnostic(
                    fact_index=fact_index,
                    citation_index=citation_index,
                    source_ref=citation.source_ref,
                    code="source_type_mismatch",
                    detail="Citation source_type does not match the bounded source.",
                ))
                continue
            quote = normalize_exact_text(citation.quote)
            source_text = normalize_exact_text(source.text)
            accepted_quote = citation.quote
            if quote not in source_text:
                repaired_quote = recover_exact_quote(source.text, citation.quote)
                if repaired_quote is None:
                    diagnostics.append(CitationDiagnostic(
                        fact_index=fact_index,
                        citation_index=citation_index,
                        source_ref=citation.source_ref,
                        code="quote_not_exact",
                        detail="Quote is not an exact substring after NFC and line-ending normalization.",
                    ))
                    continue
                accepted_quote = repaired_quote
                diagnostics.append(CitationDiagnostic(
                    fact_index=fact_index,
                    citation_index=citation_index,
                    source_ref=citation.source_ref,
                    code="quote_repaired",
                    detail=(
                        "Citation whitespace was rebound to the unique exact source span; "
                        "the stored citation remains verbatim source text."
                    ),
                ))

            modality = citation.modality
            if not source.eligible_current_evidence:
                modality = EvidenceModality.QUOTED_HISTORY
                diagnostics.append(CitationDiagnostic(
                    fact_index=fact_index,
                    citation_index=citation_index,
                    source_ref=citation.source_ref,
                    code="ineligible_current_source",
                    detail="Exact quote retained but forced to quoted_history for state evaluation.",
                ))
            bound_citation = EvidenceCitation(
                source_type=citation.source_type,
                source_ref=source.source_ref,
                quote=accepted_quote,
                actor=source.actor,
                event_at=source.event_at,
                evidence_level=source.evidence_level,
                polarity=citation.polarity,
                modality=modality,
            )
            if not case.synthetic and source.source_type == EvidenceSourceType.EMAIL:
                from src.email.source_authority import bound_pct_modality
                bound_citation = bound_pct_modality(fact.predicate, bound_citation, source.text)
            bounded.append(bound_citation)
            accepted += 1

        accepted_facts.append(ExtractedProgressFact(
            predicate=fact.predicate,
            value=fact.value,
            subject=fact.subject,
            citations=bounded,
            extraction_confidence=fact.extraction_confidence,
        ))

    identity_resolved = bool(
        extraction.project_identity_resolved
        and extraction.resolved_project_id == case.project_id
        and case.project_identity_resolved_hint
    )
    return ValidatedEmailEvidence(
        project_identity_resolved=identity_resolved,
        resolved_project_id=case.project_id if identity_resolved else None,
        accepted_facts=accepted_facts,
        rejected_citations=diagnostics,
        source_hashes={source.source_ref: source.sha256 for source in case.sources},
        total_citations=total,
        accepted_citations=accepted,
    )


def unique_source_quotes(facts: list[ExtractedProgressFact]) -> list[EvidenceCitation]:
    seen: set[tuple[str, str]] = set()
    values: list[EvidenceCitation] = []
    for fact in facts:
        for citation in fact.citations:
            key = (citation.source_ref, citation.quote)
            if key in seen:
                continue
            seen.add(key)
            values.append(EvidenceCitation.model_validate(citation.model_dump()))
    return values
