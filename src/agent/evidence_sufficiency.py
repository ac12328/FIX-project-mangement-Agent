"""Deterministic stopping policy once a canonical document satisfies the task."""

from __future__ import annotations

import re
from typing import Any, Iterable

from src.agent.tool_receipts import ToolExecutionReceipt


_SINGLE_RESOURCE_HINTS = re.compile(
    r"(?:\bguidelines?\b|\bguide\b|\bprocedure\b|\bplaybook\b|"
    r"\bpolicy\b|\bmanual\b|\bchecklist\b|\bfull text\b|\bwiki\b|"
    r"\u6307\u5f15|\u6307\u5357|\u6d41\u7a0b|\u898f\u7bc4|"
    r"\u624b\u518a|\u5168\u6587)",
    re.IGNORECASE,
)
_MULTI_SOURCE_HINTS = re.compile(
    r"(?:\bcompare\b|\bcomparison\b|\bversus\b|\bvs\.?\b|"
    r"\blatest\b|\bmost recent\b|\btoday(?:'s)?\b|\bnews\b|"
    r"\bmultiple sources?\b|\bcross[- ]?check\b|\u6bd4\u8f03|"
    r"\u6700\u65b0|\u591a\u500b\u4f86\u6e90|\u4ea4\u53c9\u6838\u5c0d)",
    re.IGNORECASE,
)


def _flatten_receipts(
    receipts: Iterable[ToolExecutionReceipt],
) -> list[ToolExecutionReceipt]:
    flattened: list[ToolExecutionReceipt] = []
    pending = list(reversed(list(receipts)))
    while pending:
        receipt = pending.pop()
        flattened.append(receipt)
        pending.extend(reversed(receipt.child_receipts))
    return flattened


def canonical_wiki_evidence(
    receipts: Iterable[ToolExecutionReceipt],
) -> list[tuple[str, str | None]]:
    """Return canonical full-document Wiki locators and bounded excerpts."""
    found: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for receipt in _flatten_receipts(receipts):
        if receipt.status != "succeeded" or receipt.tool_name != "wiki_read":
            continue
        for item in receipt.evidence:
            if (
                item.source_type == "wiki"
                and item.authority == "canonical"
                and "full_document" in item.claim_coverage
                and item.locator not in seen
            ):
                seen.add(item.locator)
                found.append((item.locator, item.excerpt))
    return found


def should_stop_after_canonical_wiki(
    *,
    specialist_type: str | None,
    user_text: str,
    receipts: Iterable[ToolExecutionReceipt],
) -> bool:
    """Stop simple research-document retrieval once trusted full text exists."""
    if str(specialist_type or "").casefold() != "research":
        return False
    text = str(user_text or "")
    if not _SINGLE_RESOURCE_HINTS.search(text) or _MULTI_SOURCE_HINTS.search(text):
        return False
    return bool(canonical_wiki_evidence(receipts))


def evidence_sufficient_message(locator: str) -> dict[str, Any]:
    """Provide a deterministic tool-free synthesis instruction to the model."""
    return {
        "role": "system",
        "content": (
            "<evidence-sufficiency status=\"satisfied\">\n"
            f"Canonical trusted Wiki full text was retrieved from {locator}. "
            "Retrieval is complete: do not call more tools. Synthesize the final "
            "answer from retained evidence. Source precedence is canonical trusted "
            "Wiki > project evidence > database > approved reference > learned rules. "
            "Never describe the canonical page as unread when its successful receipt "
            "is present.\n</evidence-sufficiency>"
        ),
    }
