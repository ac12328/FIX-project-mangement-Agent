"""Deterministic evidence extraction helpers for project briefings."""

from __future__ import annotations

import re
from datetime import datetime, timezone

_TIME_AMPM = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", re.I)
_TIME_24H = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
_ISO_DATE = re.compile(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b")
_DMY_DATE = re.compile(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](20\d{2}|\d{2}))?\b")
_PCT_CONTEXT = re.compile(
    r"\b(?:pct|fix\s+(?:connectivity|connection)"
    r"(?:(?:\s*/\s*|\s+and\s+)order)?\s+(?:test|testing)|"
    r"connectivity\s*/\s*order\s+(?:test|testing))\b",
    re.I,
)
_TELNET_SUCCESS = re.compile(
    r"\b(success|successful|succeeded|passed|working|connected)\b", re.I
)
_CLAUSE_SPLIT = re.compile(r"(?:\r?\n)+|(?<=[.!?])\s+")


def extract_telnet_results(text: str) -> tuple[bool, bool, bool]:
    """Return primary, backup, and unscoped telnet success signals.

    Success terms are evaluated within the same sentence or line as the telnet
    reference so a request to enable backup does not inherit a primary success.
    """
    primary = False
    backup = False
    generic = False
    for clause in _CLAUSE_SPLIT.split(text):
        lower = clause.lower().strip()
        if not lower or "telnet" not in lower or not _TELNET_SUCCESS.search(lower):
            continue
        clause_primary = any(token in lower for token in ("primary", "76.39", "10.91"))
        clause_backup = any(token in lower for token in ("backup", "76.40", "10.92"))
        primary = primary or clause_primary
        backup = backup or clause_backup
        generic = generic or not (clause_primary or clause_backup)
    return primary, backup, generic


def is_pct_confirmation(text: str) -> bool:
    """Return whether text records a confirmed PCT appointment."""
    lower = text.lower()
    if "pct" not in lower:
        return False
    return any(
        phrase in lower
        for phrase in (
            "pct confirmed",
            "confirmed pct",
            "can take the pct",
            "we can take the pct",
            "scheduled the pct",
            "pct is scheduled",
            "booked the pct",
        )
    )


def is_pct_proposal(text: str, options: list[str]) -> bool:
    """Return whether candidate appointment options are proposals, not confirmations."""
    lower = text.lower()
    if not options or not _has_pct_context(lower) or is_pct_confirmation(lower):
        return False
    if len(options) > 1:
        return True
    return any(
        phrase in lower
        for phrase in (
            "propose",
            "available",
            "either",
            "option",
            "please advise",
            "can have",
            "could have",
        )
    )


def extract_pct_datetimes(text: str, occurred_at: str) -> list[str]:
    """Extract unique PCT appointment candidates rendered in Hong Kong time."""
    if not _has_pct_context(text):
        return []

    time_match = _TIME_AMPM.search(text)
    if time_match:
        hour = int(time_match.group(1)) % 12
        minute = int(time_match.group(2) or 0)
        if time_match.group(3).lower() == "pm":
            hour += 12
    else:
        time_match = _TIME_24H.search(text)
        if not time_match:
            return []
        hour, minute = map(int, time_match.groups())

    date_parts: list[tuple[int, int, int]] = []
    iso_matches = list(_ISO_DATE.finditer(text))
    if iso_matches:
        date_parts.extend(tuple(map(int, match.groups())) for match in iso_matches)
    else:
        default_year = _year_from_timestamp(occurred_at)
        for match in _DMY_DATE.finditer(text):
            day = int(match.group(1))
            month = int(match.group(2))
            raw_year = match.group(3)
            year = int(raw_year) if raw_year else default_year
            if year < 100:
                year += 2000
            date_parts.append((year, month, day))

    values: list[str] = []
    for year, month, day in date_parts:
        try:
            value = datetime(year, month, day, hour, minute)
        except ValueError:
            continue
        rendered = value.strftime("%Y-%m-%d %H:%M HKT")
        if rendered not in values:
            values.append(rendered)
    return values


def _has_pct_context(text: str) -> bool:
    return bool(_PCT_CONTEXT.search(text))


def _year_from_timestamp(value: str) -> int:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).year
    except (TypeError, ValueError):
        return datetime.now(timezone.utc).year
