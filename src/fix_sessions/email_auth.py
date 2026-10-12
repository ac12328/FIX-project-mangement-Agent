"""Parse email authentication evidence used by FIX configuration policy."""
from __future__ import annotations

import re
from email.utils import parseaddr
from typing import Iterable, Mapping

from .models import AuthResult, EmailAuthentication

_RESULT_RE = {
    "spf": re.compile(r"\bspf=(pass|fail|softfail|neutral|none|temperror|permerror)\b", re.I),
    "dkim": re.compile(r"\bdkim=(pass|fail|neutral|none|temperror|permerror)\b", re.I),
    "dmarc": re.compile(r"\bdmarc=(pass|fail|bestguesspass|none|temperror|permerror)\b", re.I),
}
_DOMAIN_RE = re.compile(r"(?:smtp\.mailfrom|header\.from|header\.d)\s*=\s*([^;\s]+)", re.I)


def _result(value: str | None) -> AuthResult:
    return AuthResult.PASS if value and value.lower() in {"pass", "bestguesspass"} else (AuthResult.FAIL if value and value.lower() != "none" else AuthResult.UNKNOWN)


def _domain(address: str | None) -> str | None:
    parsed = parseaddr(address or "")[1]
    return parsed.rsplit("@", 1)[1].lower().rstrip(".") if "@" in parsed else None


def _aligned(auth_domain: str | None, sender_domain: str | None) -> bool:
    if not auth_domain or not sender_domain:
        return False
    auth_domain = auth_domain.lower().rstrip(".")
    return auth_domain == sender_domain or auth_domain.endswith("." + sender_domain) or sender_domain.endswith("." + auth_domain)


def parse_email_authentication(headers: Mapping[str, str] | Iterable[Mapping[str, str]], sender_email: str | None) -> EmailAuthentication:
    """Return conservative SPF/DKIM/DMARC results and From-domain alignment."""
    if isinstance(headers, Mapping):
        normalized = {str(k).lower(): str(v) for k, v in headers.items()}
    else:
        normalized = {str(h.get("name", "")).lower(): str(h.get("value", "")) for h in headers}
    raw = "\n".join(v for k, v in normalized.items() if k in {"authentication-results", "arc-authentication-results"})
    matches = {name: regex.search(raw) for name, regex in _RESULT_RE.items()}
    sender_domain = _domain(sender_email)
    auth_domains = _DOMAIN_RE.findall(raw)
    return EmailAuthentication(
        spf=_result(matches["spf"].group(1) if matches["spf"] else None),
        dkim=_result(matches["dkim"].group(1) if matches["dkim"] else None),
        dmarc=_result(matches["dmarc"].group(1) if matches["dmarc"] else None),
        aligned_sender=any(_aligned(domain.strip("<>"), sender_domain) for domain in auth_domains),
        raw_summary=None,
    )
