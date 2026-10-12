"""Pure presentation intent decision for the A inbound analysis path.

This module does not prepare payloads, persist outcomes, or dispatch notifications.
"""
from __future__ import annotations

from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict


_FAILED_WRITES = frozenset({"review_persistence_failed", "field_route_failed"})
_OWN_PRESENTATION = frozenset({
    "review_card", "review_notification_pending", "review_notification_unknown",
    "auto_applied_undo_card", "auto_applied_notification_pending",
    "auto_applied_notification_unknown", "direct_write",
})


class AnalysisPresentationDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    processing_succeeded: bool
    presentation_kind: Literal["info_card", "plain"] | None
    reply: str


def decide_analysis_presentation(
    *, write_action: str | None, stored: Mapping[str, Any] | None, reply: str,
) -> AnalysisPresentationDecision:
    """Select only the A result's presentation intent, not its write effects.

    Reviews and applied updates own their own cards. Failed field/review
    persistence is incomplete, not an informational or successful plain route.
    """
    succeeded = write_action not in _FAILED_WRITES
    kind: Literal["info_card", "plain"] | None = None
    if succeeded:
        if not stored:
            if reply and reply != "I'm not sure how to help with that.":
                kind = "plain"
        elif write_action not in _OWN_PRESENTATION:
            if stored.get("summary"):
                kind = "info_card"
            elif reply and reply != "I'm not sure how to help with that.":
                kind = "plain"
    return AnalysisPresentationDecision(
        processing_succeeded=succeeded, presentation_kind=kind, reply=reply,
    )
