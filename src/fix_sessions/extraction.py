"""Deterministic extraction of candidate FIX session configuration."""
from __future__ import annotations

import re
from typing import Any

_SECRET_PATTERNS = [
    re.compile(r"(?im)^\s*(?:password|passwd|pwd)\s*[:=]\s*([^\s,;]+)"),
    re.compile(r"(?im)^\s*(?:username|user\s*id|login)\s*[:=]\s*([^\s,;]+)"),
]
_LABELS = {
    "sender_comp_id": re.compile(r"(?im)^\s*SenderCompID\s*[:=]\s*([^\s,;]+)"),
    "target_comp_id": re.compile(r"(?im)^\s*TargetCompID\s*[:=]\s*([^\s,;]+)"),
    "begin_string": re.compile(r"(?im)^\s*(?:BeginString|FIX\s*Version)\s*[:=]\s*(FIXT?\.[0-9.]+)"),
    "heartbeat_seconds": re.compile(r"(?im)^\s*(?:HeartBtInt|Heartbeat(?:\s*Interval)?)\s*[:=]\s*(\d+)"),
}
_ENDPOINT = re.compile(r"(?im)^\s*(?:(primary|backup|dr)\s+)?(?:host|ip|endpoint)\s*[:=]\s*([A-Za-z0-9.-]+)(?:\s*[,;/ ]\s*|\s+port\s*[:=]\s*)(\d{1,5})")
_ENV = re.compile(r"\b(UAT|TEST|PROD(?:UCTION)?)\b", re.I)


def redact_secrets(text: str) -> tuple[str, dict[str, str]]:
    found: dict[str, str] = {}
    redacted = text
    for pattern in _SECRET_PATTERNS:
        def replace(match: re.Match[str]) -> str:
            label = "password" if "pass" in match.group(0).lower() or "pwd" in match.group(0).lower() else "username"
            found[label] = match.group(1)
            return match.group(0).replace(match.group(1), "[REDACTED]")
        redacted = pattern.sub(replace, redacted)
    return redacted, found


def extract_session_update(text: str, *, environment: str | None = None) -> dict[str, Any]:
    """Extract one environment only; ambiguous mixed UAT/Production text is rejected."""
    env_tokens = {m.group(1).upper() for m in _ENV.finditer(text)}
    normalized = {"UAT" if x in {"UAT", "TEST"} else "PRODUCTION" for x in env_tokens}
    if environment:
        selected = "PRODUCTION" if environment.upper() in {"PROD", "PRODUCTION"} else "UAT"
    elif len(normalized) == 1:
        selected = next(iter(normalized))
    elif len(normalized) > 1:
        raise ValueError("mixed UAT and Production configuration requires separate evidence blocks")
    else:
        raise ValueError("FIX environment is missing")
    clean, secrets = redact_secrets(text)
    update: dict[str, Any] = {"environment": selected, "endpoints": []}
    for field, pattern in _LABELS.items():
        match = pattern.search(clean)
        if match:
            update[field] = int(match.group(1)) if field == "heartbeat_seconds" else match.group(1).strip()
    for index, match in enumerate(_ENDPOINT.finditer(clean), 1):
        port = int(match.group(3))
        if port > 65535:
            raise ValueError("invalid FIX endpoint port")
        update["endpoints"].append({"route_role": (match.group(1) or "primary").lower(), "host": match.group(2), "port": port, "priority": index})
    update["secrets"] = secrets
    return update
