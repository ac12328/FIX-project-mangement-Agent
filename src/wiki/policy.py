"""Containment policies for wiki retrieval and sensitive content."""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class SensitiveFinding:
    """A redacted sensitive-content finding."""

    line: int
    kind: str


_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "password",
        re.compile(
            r"(?i)\b(?:password|passwd|pwd)\b\s*[:=]\s*`?\s*[^\s`]+"
        ),
    ),
    (
        "private_key",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    ),
    (
        "access_token",
        re.compile(
            r"(?i)\b(?:access[_ -]?token|api[_ -]?key|secret[_ -]?key)\b"
            r"\s*[:=]\s*`?\s*[^\s`]+"
        ),
    ),
)

_IDENTIFIER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ip_address", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    (
        "fix_session_identifier",
        re.compile(r"(?i)\b(?:sender|target)(?:comp)?id\b"),
    ),
    (
        "account_identifier",
        re.compile(
            r"(?i)(?:\btag\s*1\b|\baccount(?:\s+code)?\b)"
            r"(?:\s*(?:\([^)]*\))?\s*[:=]\s*|\s+(?:is|must\s+be|should\s+be)\s+)"
            r"`?[A-Z0-9][A-Z0-9._-]{2,}`?"
        ),
    ),
)


def scan_sensitive_text(text: str, *, include_identifiers: bool = False) -> list[SensitiveFinding]:
    """Return redacted findings without retaining matched values."""
    patterns = list(_SECRET_PATTERNS)
    if include_identifiers:
        patterns.extend(_IDENTIFIER_PATTERNS)

    findings: list[SensitiveFinding] = []
    seen: set[tuple[int, str]] = set()
    for line_no, line in enumerate((text or "").splitlines(), start=1):
        for kind, pattern in patterns:
            if pattern.search(line):
                key = (line_no, kind)
                if key not in seen:
                    seen.add(key)
                    findings.append(SensitiveFinding(line=line_no, kind=kind))
    return findings


def contains_secret(text: str) -> bool:
    """Return whether text contains a secret-like value."""
    return bool(scan_sensitive_text(text))


def is_external_safe_page(
    *,
    visibility: str,
    visibility_explicit: bool,
    status: str,
    content: str,
) -> bool:
    """Apply fail-closed eligibility for external-facing retrieval.

    Legacy pages that omitted visibility are treated as internal until they are
    explicitly reviewed during the WikiPage v2 migration.
    """
    if not visibility_explicit or visibility != "public":
        return False
    if status not in {"active", "trusted"}:
        return False
    return not scan_sensitive_text(content, include_identifiers=True)
