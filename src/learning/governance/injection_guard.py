"""Deterministic prompt-injection and prohibited-instruction indicators."""
from __future__ import annotations

import re

_INJECTION = (
    r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions?",
    r"reveal\s+(the\s+)?(system|developer)\s+prompt",
    r"act\s+as\s+(the\s+)?system",
    r"override\s+(the\s+)?(system|developer|safety)\s+instructions?",
    r"jailbreak",
)
_POLICY_CHANGE = (
    r"disable\s+(operator\s+)?confirmation",
    r"bypass\s+(operator\s+)?confirmation",
    r"grant\s+.*tool.*permission",
    r"change\s+.*tool.*permission",
    r"modify\s+.*(system\s+prompt|permission|allowlist|safety\s+policy)",
    r"weaken\s+.*(safe\s+write|review|guard)",
)
_EXFILTRATION = (
    r"upload\s+.*(?:files?|data|credentials?)\s+to\s+",
    r"send\s+.*(?:secret|credential|client\s+data).*\s+to\s+",
    r"exfiltrat",
)
_UNTRUSTED_PROCEDURE = (
    r"follow\s+the\s+instructions?\s+in\s+(?:this|the)\s+(?:customer|client|external)\s+(?:email|message|wiki)",
    r"treat\s+.*(?:email|message|wiki).*as\s+(?:the\s+)?(?:new\s+)?(?:procedure|runbook|policy)",
)


def indicator_codes(text: str, *, target_type: str) -> set[str]:
    normalized = " ".join(text.lower().split())
    codes: set[str] = set()
    if any(re.search(pattern, normalized) for pattern in _INJECTION):
        codes.add("possible_prompt_injection")
    if any(re.search(pattern, normalized) for pattern in _POLICY_CHANGE):
        codes.add("unsupported_policy_change")
    if any(re.search(pattern, normalized) for pattern in _EXFILTRATION):
        codes.add("exfiltration_instruction")
    if target_type == "procedure" and any(re.search(pattern, normalized) for pattern in _UNTRUSTED_PROCEDURE):
        codes.add("untrusted_instruction_as_procedure")
    return codes
