"""
Support signal detection for email triage.

Detects whether an inbound email is about a technical support issue
(FIX session errors, network problems, cert renewals, etc.) rather
than a project onboarding update.
"""

import re
import sqlite3

# Keywords that signal a support/troubleshooting email
SUPPORT_SIGNAL_KEYWORDS = {
    # FIX session issues
    "session down", "session dropped", "session disconnect",
    "logon failed", "logon rejected", "logon error",
    "heartbeat timeout", "heartbeat failure",
    "sequence gap", "sequence reset", "resend request",
    "fix error", "fix reject",
    # Network issues
    "connection refused", "connection timeout", "connection lost",
    "port blocked", "firewall block", "network unreachable",
    "latency spike", "packet loss",
    # Certificate issues
    "cert expired", "certificate expired", "cert renewal",
    "certificate renewal", "ssl error", "tls error",
    "cert expiry", "certificate expiry",
    # Trade/post-trade issues
    "order reject", "order rejected", "execution report error",
    "order not allowed", "not allowed for account",
    "short sell order", "short sell not allowed",
    "trade break", "trade mismatch", "allocation fail",
    "settlement fail", "position mismatch",
    # General support language
    "error message", "issue with", "problem with",
    "not working", "unable to connect", "cannot connect",
    "outage", "incident", "downtime", "degraded",
    "urgent issue", "critical issue", "production issue",
}

# Subject patterns strongly indicating support (regex)
SUPPORT_SUBJECT_PATTERNS = [
    re.compile(r"\b(issue|error|problem|outage|incident|alert)\b", re.IGNORECASE),
    re.compile(r"\bfix\s+session\s+(down|drop|error|fail)", re.IGNORECASE),
    re.compile(r"\bcert(ificate)?\s+(expir|renew)", re.IGNORECASE),
    re.compile(r"\bconnect(ion|ivity)\s+(fail|error|timeout|refused|lost)", re.IGNORECASE),
]

SUPPORT_SIGNAL_THRESHOLD = 2


def count_support_signals(subject: str, body: str) -> int:
    """Count how many support signal keywords appear in subject + body."""
    combined = (subject + " " + body[:800]).lower()
    count = sum(1 for kw in SUPPORT_SIGNAL_KEYWORDS if kw in combined)
    # Also count subject pattern matches
    for pat in SUPPORT_SUBJECT_PATTERNS:
        if pat.search(subject):
            count += 1
    return count


def detect_support_type(
    conn: sqlite3.Connection,
    sender_email: str,
    subject: str,
    body: str,
) -> str | None:
    """
    Detect whether an email is a support issue.

    Returns:
        "support_issue" — standalone support (no linked project, or existing B2B)
        "project_support" — support issue linked to an active project
        None — not a support email
    """
    signal_count = count_support_signals(subject, body)
    if signal_count < 2:
        return None

    # Check if sender is linked to an active project
    from src.triage.identity_resolution import resolve_sender
    identity = resolve_sender(conn, sender_email)
    if identity.project_ids:
        return "project_support"
    return "support_issue"
