"""Deterministic secret, credential, PII, and sensitivity checks."""
from __future__ import annotations

import re

_SECRET_PATTERNS = (
    re.compile(r"[\"']?(?:password|passwd|api[_ -]?key|secret|access[_ -]?token|refresh[_ -]?token)[\"']?\s*[:=]\s*[\"']?[^\"'\s][^\"'}]*", re.I),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)
_PII_PATTERNS = (
    re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I),
    re.compile(r"\b(?:\+?\d[\d ()-]{7,}\d)\b"),
    re.compile(r"\b[A-Z]{1,2}\d{6}\([0-9A]\)\b", re.I),
)


def sensitivity_codes(text: str, *, observation_sensitivities: set[str], target_type: str,
                      allowed: set[str]) -> set[str]:
    codes: set[str] = set()
    if any(pattern.search(text) for pattern in _SECRET_PATTERNS):
        codes.add("contains_secret")
    if any(pattern.search(text) for pattern in _PII_PATTERNS):
        codes.add("contains_pii")
    if any(value not in allowed for value in observation_sensitivities):
        codes.add("sensitivity_not_allowed")
    return codes
