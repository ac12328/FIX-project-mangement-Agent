"""Classify knowledge nuggets for the wiki compiler queue.

Account-lifecycle and market-calendar facts stay in the nugget log.
They must not be compiled or later injected into the agent.
"""
from __future__ import annotations

import re
from typing import Any

SKIP_ACCOUNT_LIFECYCLE = "account_lifecycle"
SKIP_MARKET_CALENDAR = "market_calendar"
SKIP_BAU_OPS = "bau_ops"
SKIP_LOW_CONFIDENCE = "low_confidence"

_ACCOUNT_LIFECYCLE_RE = re.compile(
    r"("
    r"\bAML\b|"
    r"account\s*opening|"
    r"re-?activat|"
    r"credit\s*line|"
    r"sub[- ]?account|"
    r"additional\s+bank\s+account|"
    r"\bCH37\b|"
    r"NAR1|"
    r"letter\s+of\s+guarantee|"
    r"corporate\s+shareholder"
    r")",
    re.IGNORECASE,
)

_CALENDAR_RE = re.compile(
    r"("
    r"holiday\s+calendar|"
    r"public\s+holiday|"
    r"settlement\s+holiday|"
    r"trading\s+holiday|"
    r"customary\s+holiday|"
    r"\bholiday\s+types\b"
    r")",
    re.IGNORECASE,
)

_BAU_OPS_RE = re.compile(
    r"("
    r"\bwithdrawal\b|"
    r"fund\s*movement|"
    r"authorized\s+(person|dealer|trader)|"
    r"美股單|"
    r"corporate\s+action|"
    r"email\s+domain\s+migration"
    r")",
    re.IGNORECASE,
)

_TOPIC_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("hk-ipo", re.compile(r"\b(HK\s*IPO|FINI|BCAN)\b", re.IGNORECASE)),
    ("korea-algo", re.compile(r"\bkorea\b.*\balgo\b|\balgo\b.*\bkorea\b", re.IGNORECASE)),
    ("fix-connectivity", re.compile(
        r"\b(FIX|CompID|logon|VPN|PCT|session|conformance|UAT)\b", re.IGNORECASE
    )),
    ("settlement", re.compile(
        r"\b(SSI|DVP|DTC|QI|settlement|custodian)\b", re.IGNORECASE
    )),
)


def classify_nugget(
    *,
    title: str,
    content: str,
    confidence: str | None = None,
    domain: str | None = None,
    knowledge_type: str | None = None,
) -> tuple[str, str | None]:
    """Return (topic, skip_reason). skip_reason is None when the nugget may compile."""
    _ = knowledge_type
    if (confidence or "").strip().lower() == "low":
        return "skip", SKIP_LOW_CONFIDENCE
    text = f"{title}\n{content}\n{domain or ''}"
    if _ACCOUNT_LIFECYCLE_RE.search(text):
        return "skip", SKIP_ACCOUNT_LIFECYCLE
    if _CALENDAR_RE.search(text):
        return "skip", SKIP_MARKET_CALENDAR
    if _BAU_OPS_RE.search(text):
        return "skip", SKIP_BAU_OPS
    for topic, pattern in _TOPIC_RULES:
        if pattern.search(text):
            return topic, None
    if domain in {"fix-connectivity", "conformance", "settlement", "b2b"}:
        return domain, None
    return "general", None


def classify_row(row: dict[str, Any]) -> tuple[str, str | None]:
    return classify_nugget(
        title=str(row.get("title") or ""),
        content=str(row.get("content") or ""),
        confidence=row.get("confidence"),
        domain=row.get("domain"),
        knowledge_type=row.get("knowledge_type"),
    )
