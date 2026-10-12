"""Deterministic recovery variants for Policy-v4 holdout questions.

These variants are evaluator-only inputs. They do not affect production serving policy.
"""

from __future__ import annotations

from typing import Any, Iterator, Sequence

_RECOVERY_TEMPLATE_INDEXES = (5, 0, 3, 11)
_RECOVERY_FORMATS = (
    "Start with a one-line portfolio takeaway, then provide detailed cited findings and keep proposed follow-up in a separate section.",
    "List evidence-backed findings first, then evidence gaps, then operator recommendations.",
    "Give an executive summary before a compact evidence table, followed by a separate operator-attention list.",
    "Use headings for recorded facts, unresolved evidence gaps, and proposed next actions.",
    "Return a handover-ready evidence table and then clearly distinguish recommendations from recorded state.",
    "Present factual findings before follow-up suggestions and cite each factual sentence.",
    "Separate the recorded portfolio state from proposed actions in every section.",
    "End with a concise attention list after the detailed cited evidence.",
    "Use a compact table for findings, then a separate uncertainty and recommendation section.",
    "Provide a short takeaway, detailed evidence, and clearly labelled recommendations in that order.",
)


def repair_recovery_questions(
    *,
    portfolio_templates: Sequence[str],
    evidence_lenses: Sequence[str],
    modifiers: Sequence[str],
) -> Iterator[dict[str, Any]]:
    """Yield zero-overlap, repair-rich portfolio requests for fresh experiments."""
    for template_index in _RECOVERY_TEMPLATE_INDEXES:
        template = portfolio_templates[template_index]
        for lens in evidence_lenses:
            for output_format in _RECOVERY_FORMATS:
                yield {
                    "question": f"{template} {lens} {modifiers[2]} {output_format}",
                    "scope_mode": "portfolio",
                    "project_ids": [],
                    "web_enabled": False,
                    "intent_type": "aggregate_query",
                }


__all__ = ["repair_recovery_questions"]
