"""Receipt-grounded correction for model claims about agent/tool execution."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable


_TIMEOUT_TWICE = re.compile(
    r"(?:\u5169\u6b21|\u4e8c\u6b21|2\s*\u6b21).{0,16}"
    r"(?:\u903e\u6642|\u8d85\u6642)|"
    r"(?:research\s+specialist|specialist).{0,40}"
    r"(?:timed?\s*out|timeouts?).{0,16}(?:twice|two|2)|"
    r"(?:twice|two|2).{0,16}(?:timed?\s*out|timeouts?)",
    re.IGNORECASE,
)
_UNREAD_FULL_TEXT = re.compile(
    r"(?:\u672a\u80fd|\u7121\u6cd5|\u6c92\u6709|\u4e26\u672a).{0,20}"
    r"(?:\u8b80|\u53d6\u5f97|\u6293\u53d6).{0,12}"
    r"(?:\u5168\u6587|\u5b8c\u6574\u5167\u5bb9)|"
    r"(?:could\s+not|couldn['\u2019]?t|unable\s+to|failed\s+to).{0,24}"
    r"(?:read|retrieve|load).{0,20}"
    r"(?:full\s+text|full\s+content|entire\s+(?:page|document))",
    re.IGNORECASE,
)
_SENTENCE_BOUNDARY = re.compile(
    r"(?<=[\u3002\uff01\uff1f.!?])|(?=\n)|(?<=\n)"
)


@dataclass(frozen=True)
class ProcessNarrativeResult:
    text: str
    correction_codes: tuple[str, ...] = ()


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


def _canonical_wiki_locator(receipts: Iterable[Any]) -> str | None:
    for receipt in _flatten_receipts(receipts):
        if (
            getattr(receipt, "status", None) != "succeeded"
            or getattr(receipt, "tool_name", None) != "wiki_read"
        ):
            continue
        for item in getattr(receipt, "evidence", ()) or ():
            if (
                getattr(item, "source_type", None) == "wiki"
                and getattr(item, "authority", None) == "canonical"
                and "full_document" in (getattr(item, "claim_coverage", ()) or ())
            ):
                return str(getattr(item, "locator", "") or "") or None
    return None


def _correction_text(
    *,
    delegate_count: int,
    canonical_locator: str | None,
    false_timeout: bool,
    false_unread: bool,
    chinese: bool,
) -> str:
    """Describe only receipt facts that directly correct the detected claim."""
    parts: list[str] = []
    if chinese:
        if false_timeout:
            if delegate_count == 1:
                parts.append(
                    "\u57f7\u884c\u7d00\u9304\u53ea\u986f\u793a\u4e00\u6b21 specialist "
                    "\u59d4\u6d3e\uff0c\u4e0d\u652f\u6301\u300c\u5169\u6b21 specialist "
                    "\u903e\u6642\u300d\u7684\u6558\u8ff0"
                )
            else:
                parts.append(
                    "\u57f7\u884c\u7d00\u9304\u672a\u986f\u793a\u5169\u6b21 specialist "
                    "\u59d4\u6d3e\uff0c\u4e0d\u652f\u6301\u300c\u5169\u6b21 specialist "
                    "\u903e\u6642\u300d\u7684\u6558\u8ff0"
                )
        if false_unread and canonical_locator is not None:
            parts.append(
                "\u5df2\u6210\u529f\u8b80\u53d6 canonical Wiki \u5168\u6587\uff08"
                f"{canonical_locator}"
                "\uff09\uff0c\u5f8c\u7e8c\u903e\u6642\u4e26\u672a\u4f7f\u5df2\u53d6"
                "\u5f97\u7684\u8b49\u64da\u5931\u6548"
            )
        return "\uff1b".join(parts) + "\u3002"

    if false_timeout:
        if delegate_count == 1:
            parts.append(
                "Execution receipts show one specialist delegation, not two "
                "specialist timeouts"
            )
        else:
            parts.append(
                "Execution receipts do not support a claim of two specialist timeouts"
            )
    if false_unread and canonical_locator is not None:
        parts.append(
            "the full canonical Wiki page was successfully read "
            f"({canonical_locator}), and a later timeout did not erase the evidence"
        )
    return "; ".join(parts) + "."


def correct_process_narrative(
    candidate: str,
    *,
    receipts: Iterable[Any],
) -> ProcessNarrativeResult:
    """Correct timeout/read claims only when receipts deterministically contradict them."""
    top_level = list(receipts)
    delegate_count = sum(
        getattr(receipt, "tool_name", None) == "delegate"
        for receipt in top_level
    )
    canonical_locator = _canonical_wiki_locator(top_level)
    segments = _SENTENCE_BOUNDARY.split(str(candidate or ""))
    segment_flags = [
        (
            delegate_count < 2 and bool(_TIMEOUT_TWICE.search(segment)),
            canonical_locator is not None and bool(_UNREAD_FULL_TEXT.search(segment)),
        )
        for segment in segments
    ]
    any_false_timeout = any(timeout for timeout, _ in segment_flags)
    any_false_unread = any(unread for _, unread in segment_flags)
    if not any_false_timeout and not any_false_unread:
        return ProcessNarrativeResult(text=str(candidate or ""))

    codes: list[str] = []
    if any_false_timeout:
        codes.append("delegate_count_mismatch")
    if any_false_unread:
        codes.append("canonical_read_contradiction")
    correction = _correction_text(
        delegate_count=delegate_count,
        canonical_locator=canonical_locator,
        false_timeout=any_false_timeout,
        false_unread=any_false_unread,
        chinese=bool(re.search(r"[\u4e00-\u9fff]", str(candidate or ""))),
    )

    corrected: list[str] = []
    correction_inserted = False
    for segment, (false_timeout, false_unread) in zip(segments, segment_flags):
        if not false_timeout and not false_unread:
            corrected.append(segment)
            continue
        if not correction_inserted:
            corrected.append(correction)
            correction_inserted = True

    return ProcessNarrativeResult(
        text="".join(corrected),
        correction_codes=tuple(codes),
    )
