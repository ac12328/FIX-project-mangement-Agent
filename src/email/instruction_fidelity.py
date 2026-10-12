"""Deterministic fidelity checks for operator-requested email actions."""

from __future__ import annotations

import hashlib
import ipaddress
import re
import sqlite3
from dataclasses import dataclass
from typing import Iterable

_IP_CANDIDATE_RE = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
_PORT_RE = re.compile(r"\bport\s*(?:[:#]|number)?\s*(\d{1,5})\b", re.IGNORECASE)
_ENDPOINT_RE = re.compile(
    r"(?P<ip>(?:\d{1,3}\.){3}\d{1,3})\s*:\s*(?P<port>\d{1,5})"
)
_URL_RE = re.compile(r"\bhttps?://[^\s<>'\"]+", re.IGNORECASE)
_NAMED_INFRA_TARGET_RE = re.compile(
    r"\b([A-Za-z][A-Za-z0-9_.-]*(?:\s+[A-Za-z][A-Za-z0-9_.-]*){0,3}"
    r"\s+(?:VPN|gateway|server|system|endpoint|session))\b",
    re.IGNORECASE,
)
_LABELED_TARGET_RE = re.compile(
    r"\b(?:endpoint|gateway|server|system)\s+(?:named\s+)?"
    r"([A-Za-z0-9][A-Za-z0-9_.:-]{2,63})\b",
    re.IGNORECASE,
)
_SESSION_ID_RE = re.compile(
    r"\b(?:session(?:\s+id)?|session_id)\s*[:#]?\s*"
    r"([A-Za-z0-9][A-Za-z0-9_.:-]{2,63})\b",
    re.IGNORECASE,
)
_DISABLE_VERB_RE = re.compile(
    r"\b(?:disable|block|revoke|deny|disconnect|terminate|deactivate|shut\s+off)\b",
    re.IGNORECASE,
)
_REMOVE_ACCESS_RE = re.compile(
    r"\bremove\b.{0,40}\b(?:access|traffic|connection|vpn|port|session)\b",
    re.IGNORECASE | re.DOTALL,
)
_ENABLE_ACCESS_RE = re.compile(
    r"\b(?:enable|allow|grant|restore)\b.{0,40}"
    r"\b(?:access|traffic|connection|vpn|port|session)\b",
    re.IGNORECASE | re.DOTALL,
)
_NEGATION_PREFIX = (
    r"(?:do\s+not|don't|cannot|can't|should\s+not|shouldn't|must\s+not|"
    r"mustn't|not|never)"
)
_NEGATED_DISABLE_RE = re.compile(
    rf"\b{_NEGATION_PREFIX}\s+(?:please\s+)?"
    r"(?:disable|block|revoke|deny|disconnect|terminate|deactivate|remove)\b",
    re.IGNORECASE,
)
_NEGATED_ENABLE_RE = re.compile(
    rf"\b{_NEGATION_PREFIX}\s+(?:please\s+)?"
    r"(?:enable|allow|grant|restore)\b",
    re.IGNORECASE,
)
_EMAIL_ADDRESS_RE = re.compile(
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"
)

# Canonical shared mailboxes that are already authoritative in the email and
# advisory prompts. Keep this allowlist deliberately small: it is a resolver,
# not an address-guessing mechanism.
_TRUSTED_INTERNAL_RECIPIENT_ALIASES: dict[str, tuple[str, ...]] = {
    "network@demo.invalid": ("it network", "it network team", "network team"),
    "provisioning@provisioning.demo.invalid": (
        "demo provisioning support", "demo provisioning support team",
    ),
    "post-trade@demo.invalid": ("post-trade team", "post trade team"),
}

_TARGET_LEADING_STOPWORDS = frozenset({
    "a", "an", "the", "of", "to", "for", "from", "on", "via",
    "access", "traffic", "connection", "disable", "block", "revoke",
    "deny", "remove", "enable", "allow", "grant", "restore", "please",
})
_LABELED_TARGET_STOPWORDS = frozenset({
    "access", "address", "connection", "details", "for", "from", "ip",
    "is", "named", "on", "port", "the", "to", "traffic", "with",
})


def is_trusted_internal_mailbox(to_email: str | None) -> bool:
    """Return whether an address is one of the canonical shared mailboxes."""
    return (to_email or "").strip().casefold() in _TRUSTED_INTERNAL_RECIPIENT_ALIASES


@dataclass(frozen=True)
class DirectiveSpec:
    """Normalized, high-confidence requirements derived from trusted operator text."""

    operator_request_hash: str
    action_families: tuple[str, ...] = ()
    named_targets: tuple[str, ...] = ()
    ip_targets: tuple[str, ...] = ()
    port_targets: tuple[str, ...] = ()
    endpoint_targets: tuple[str, ...] = ()
    session_targets: tuple[str, ...] = ()
    unresolved_targets: tuple[str, ...] = ()

    @property
    def requires_enforcement(self) -> bool:
        return bool(self.action_families)

    @property
    def requires_clarification(self) -> bool:
        return self.requires_enforcement and bool(self.unresolved_targets)

    def audit_metadata(self) -> dict[str, object]:
        """Return provenance without raw operator, recipient, or target values."""
        return {
            "operator_request_hash": self.operator_request_hash,
            "action_families": list(self.action_families),
            "target_counts": {
                "named": len(self.named_targets),
                "ip": len(self.ip_targets),
                "port": len(self.port_targets),
                "endpoint": len(self.endpoint_targets),
                "session": len(self.session_targets),
                "unresolved": len(self.unresolved_targets),
            },
            "unresolved_categories": sorted({
                _unresolved_category(value) for value in self.unresolved_targets
            }),
        }


def _stable_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for raw in values:
        value = (raw or "").strip()
        key = value.casefold()
        if value and key not in seen:
            seen.add(key)
            result.append(value)
    return tuple(result)


def _contains_recipient_alias(text: str, alias: str) -> bool:
    """Match a trusted alias as complete words, tolerating punctuation."""
    normalized_text = " ".join(re.findall(r"[a-z0-9]+", (text or "").casefold()))
    normalized_alias = " ".join(re.findall(r"[a-z0-9]+", (alias or "").casefold()))
    if not normalized_text or not normalized_alias:
        return False
    return f" {normalized_alias} " in f" {normalized_text} "


def resolve_trusted_internal_recipient(operator_request_text: str | None) -> str | None:
    """Resolve exactly one allowlisted team mailbox from trusted operator text."""
    text = (operator_request_text or "").strip()
    if not text:
        return None

    literal_addresses = {
        value.casefold() for value in _EMAIL_ADDRESS_RE.findall(text)
    }
    trusted_literals = literal_addresses.intersection(
        _TRUSTED_INTERNAL_RECIPIENT_ALIASES
    )
    if literal_addresses - trusted_literals:
        return None

    alias_text = _EMAIL_ADDRESS_RE.sub(" ", text)
    alias_matches = {
        mailbox
        for mailbox, aliases in _TRUSTED_INTERNAL_RECIPIENT_ALIASES.items()
        if any(_contains_recipient_alias(alias_text, alias) for alias in aliases)
    }
    matches = trusted_literals.union(alias_matches)
    return next(iter(matches)) if len(matches) == 1 else None


def _valid_ips(text: str) -> tuple[str, ...]:
    valid: list[str] = []
    for candidate in _IP_CANDIDATE_RE.findall(text or ""):
        try:
            valid.append(str(ipaddress.ip_address(candidate)))
        except ValueError:
            continue
    return _unique(valid)


def _ports(text: str) -> tuple[str, ...]:
    values = [match.group("port") for match in _ENDPOINT_RE.finditer(text or "")]
    values.extend(match.group(1) for match in _PORT_RE.finditer(text or ""))
    return _unique(value for value in values if 0 < int(value) <= 65535)


def _named_infrastructure_targets(text: str) -> tuple[str, ...]:
    targets: list[str] = []
    for match in _NAMED_INFRA_TARGET_RE.finditer(text or ""):
        words = match.group(1).split()
        while len(words) > 1 and words[0].casefold() in _TARGET_LEADING_STOPWORDS:
            words.pop(0)
        if len(words) >= 2:
            targets.append(" ".join(words))
    return _unique(targets)


def _endpoint_targets(text: str) -> tuple[str, ...]:
    targets = [match.group(0).rstrip(".,;)") for match in _URL_RE.finditer(text or "")]
    for match in _LABELED_TARGET_RE.finditer(text or ""):
        value = match.group(1)
        if value.casefold() in _LABELED_TARGET_STOPWORDS:
            continue
        try:
            ipaddress.ip_address(value)
        except ValueError:
            targets.append(value)
    return _unique(targets)


def _session_targets(text: str) -> tuple[str, ...]:
    values = []
    for match in _SESSION_ID_RE.finditer(text or ""):
        value = match.group(1)
        if value.casefold() not in _LABELED_TARGET_STOPWORDS:
            values.append(value)
    return _unique(values)


def _has_disable_action(text: str) -> bool:
    candidate = _NEGATED_DISABLE_RE.sub("", text or "")
    return bool(_DISABLE_VERB_RE.search(candidate) or _REMOVE_ACCESS_RE.search(candidate))


def _has_enable_action(text: str) -> bool:
    candidate = _NEGATED_ENABLE_RE.sub("", text or "")
    return bool(_ENABLE_ACCESS_RE.search(candidate))


def _action_families(text: str) -> tuple[str, ...]:
    families: list[str] = []
    if _has_disable_action(text):
        families.append("disable_access")
    if _has_enable_action(text):
        families.append("enable_access")
    return tuple(families)


def _unresolved_category(value: str) -> str:
    lowered = (value or "").casefold()
    if lowered.startswith("port"):
        return "port"
    if lowered.startswith("network target"):
        return "network_target"
    if lowered.startswith("access target"):
        return "access_target"
    return "target"


def load_case_facts(conn: sqlite3.Connection, project_id: str) -> str:
    """Load a small deterministic evidence snapshot for target resolution."""
    parts: list[str] = []
    try:
        row = conn.execute(
            "SELECT client_name, notes, next_action FROM projects WHERE project_id=?",
            (project_id,),
        ).fetchone()
        if row:
            for index, key in enumerate(("client_name", "notes", "next_action")):
                value = row[key] if hasattr(row, "keys") else row[index]
                if value:
                    parts.append(str(value))
    except sqlite3.Error:
        pass
    try:
        rows = conn.execute(
            """SELECT subject, body_snippet FROM processed_emails
               WHERE project_id=? AND direction='inbound'
               ORDER BY processed_at DESC LIMIT 2""",
            (project_id,),
        ).fetchall()
        for row in rows:
            subject = row["subject"] if hasattr(row, "keys") else row[0]
            body = row["body_snippet"] if hasattr(row, "keys") else row[1]
            if subject:
                parts.append(str(subject))
            if body:
                parts.append(str(body))
    except sqlite3.Error:
        pass
    return "\n".join(parts)


def build_directive_spec(
    operator_request_text: str | None,
    *,
    background_context: str = "",
    case_facts: str = "",
) -> DirectiveSpec:
    """Build required action/target checks without trusting model-written arguments."""
    operator_text = (operator_request_text or "").strip()
    families = _action_families(operator_text)
    if not families:
        return DirectiveSpec(operator_request_hash=_stable_hash(operator_text))

    named_targets = _named_infrastructure_targets(operator_text)
    endpoint_targets = _endpoint_targets(operator_text)
    session_targets = _session_targets(operator_text)
    ip_targets = (
        _valid_ips(operator_text)
        or _valid_ips(background_context)
        or _valid_ips(case_facts)
    )
    port_candidates = (
        _ports(operator_text)
        or _ports(background_context)
        or _ports(case_facts)
    )
    unresolved: list[str] = []

    mentions_port = bool(re.search(r"\bport\b", operator_text, re.IGNORECASE))
    if mentions_port:
        if len(port_candidates) == 1:
            port_targets = port_candidates
        elif not port_candidates:
            port_targets = ()
            unresolved.append("port")
        else:
            port_targets = ()
            unresolved.append(
                "port (multiple candidates: " + ", ".join(port_candidates) + ")"
            )
    else:
        port_targets = port_candidates[:1] if len(port_candidates) == 1 else ()

    if len(ip_targets) > 4:
        unresolved.append("network target (too many candidate IP addresses)")
        ip_targets = ()

    has_specific_target = bool(
        named_targets
        or ip_targets
        or port_targets
        or endpoint_targets
        or session_targets
    )
    if not has_specific_target:
        unresolved.append("access target")

    return DirectiveSpec(
        operator_request_hash=_stable_hash(operator_text),
        action_families=families,
        named_targets=named_targets,
        ip_targets=ip_targets,
        port_targets=port_targets,
        endpoint_targets=endpoint_targets,
        session_targets=session_targets,
        unresolved_targets=_unique(unresolved),
    )


def _contains_ip(body: str, target: str) -> bool:
    return bool(
        re.search(
            rf"(?<![\w.]){re.escape(target)}(?![\w.])",
            body or "",
        )
    )


def _contains_identifier(body: str, target: str) -> bool:
    return bool(
        re.search(
            rf"(?<![A-Za-z0-9_.:-]){re.escape(target)}(?![A-Za-z0-9_.:-])",
            body or "",
            re.IGNORECASE,
        )
    )


def _contains_port(body: str, target: str) -> bool:
    return bool(
        re.search(
            rf"(?:\bport\s*(?:[:#]|number)?\s*|:\s*){re.escape(target)}(?!\d)",
            body or "",
            re.IGNORECASE,
        )
    )


def validate_draft_fidelity(body: str, spec: DirectiveSpec) -> list[str]:
    """Return deterministic instruction-fidelity failures for a generated body."""
    if not spec.requires_enforcement:
        return []
    issues: list[str] = []
    normalized = (body or "").casefold()

    if "disable_access" in spec.action_families and not _has_disable_action(body):
        issues.append("missing required disable/block/revoke access action")
    if "enable_access" in spec.action_families and not _has_enable_action(body):
        issues.append("missing required enable/allow/grant access action")

    for target in spec.named_targets:
        if target.casefold() not in normalized:
            issues.append(f"missing named target: {target}")
    for target in spec.ip_targets:
        if not _contains_ip(body, target):
            issues.append(f"missing IP target: {target}")
    for target in spec.port_targets:
        if not _contains_port(body, target):
            issues.append(f"missing port target: {target}")
    for target in spec.endpoint_targets:
        if target.casefold() not in normalized:
            issues.append(f"missing endpoint target: {target}")
    for target in spec.session_targets:
        if not _contains_identifier(body, target):
            issues.append(f"missing session target: {target}")
    return issues


def retry_instruction(spec: DirectiveSpec, issues: list[str]) -> str:
    """Create narrow retry feedback that cannot be mistaken for background evidence."""
    required = "; ".join(issues)
    return (
        "Rewrite the full email. The authoritative operator instruction was weakened or omitted. "
        f"Correct every item: {required}. Use a direct, polite request to the recipient. "
        "Do not replace a requested access-control action with investigate/check/review."
    )


def internal_recipient_was_requested(
    operator_request_text: str | None,
    to_email: str | None,
) -> bool:
    """Allow an internal handoff only when the trusted request names its audience."""
    text = (operator_request_text or "").casefold()
    email = (to_email or "").strip().casefold()
    if not text or "@" not in email:
        return False
    if email in text:
        return True
    trusted_aliases = _TRUSTED_INTERNAL_RECIPIENT_ALIASES.get(email)
    return bool(
        trusted_aliases
        and any(_contains_recipient_alias(text, alias) for alias in trusted_aliases)
    )
