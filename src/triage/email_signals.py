"""Deterministic signal sets used by the email triage pipeline."""

import re

# FIX protocol keywords for Stage 2 heuristic
FIX_KEYWORDS = {
    "fix", "fix4.2", "fix4.4", "sendercompid", "targetcompid",
    "logon", "heartbeat", "uat", "conformance", "certification",
    "session", "connectivity", "firewall", "go-live", "production",
    "tag35", "tag49", "tag56", "quickfix", "onixs",
}

# Known non-project sender patterns
OPERATIONAL_PATTERNS = {
    "noreply@", "no-reply@", "do-not-reply@", "donotreply@",
    "mailer-daemon@", "postmaster@",
    "newsletter@", "notification@", "alert@", "system@",
    "bounce@", "bounces@", "undeliverable@",
}

# Subject keywords that indicate operational/settlement emails (not FIX onboarding)
OPERATIONAL_SUBJECTS = {
    "inward remit", "outward remit", "payment confirmation",
    "order status notification", "settlement notification",
    "trade confirmation", "account statement", "remittance advice",
    "deposit slip", "tt charge", "account suspension",
    "order monitoring", "margin application", "order form",
    "loan", "deposit", "fund ops", "statement",
    "stock transfer", "subscription",
    "undeliverable", "delivery status notification", "delivery failure",
    "returned mail", "non delivery report",
    "交割憑單", "交割通知", "結算通知", "入金通知", "出金通知",
    # Narrowed: "grey market" removed — PM (Demo Grey Market) is a grey market product;
    # "grey market" in subject likely means PM onboarding, not an operational notice.
    # "grey market notice" / "grey market announcement" are operational.
    "grey market notice", "grey market announcement",
    "market notice", "exchange notification",
    "system maintenance", "scheduled downtime",
    "a/c termination",
    # "ipo" removed from subjects — PM conformance tests use IPO stocks 0002/0005
    # and legitimate PM client emails mention IPO. Keep only the more specific compound.
    "ipo subscription", "ipo application", "ipo allot",
    # "alert" and "monitoring" removed — too broad as substring matches;
    # they shadow legitimate FIX session alerts from clients.
}

OPERATIONAL_BODY_KEYWORDS = {
    "margin", "deposit slip", "tt charge", "settlement",
    "trade confirmation", "account statement", "remittance",
    "fund ops", "order monitoring", "loan", "order form",
    "交割憑單", "交割", "結算",
    "undeliverable", "returned to sender",
    # "ipo" removed — generic conformance mail may discuss IPO instruments;
    # "ipo" alone in body would suppress all PM UAT emails. Too broad.
}

INTERNAL_OPERATIONAL_SENDER_PATTERNS = {
    "settlement@", "backoffice@", "fundops@", "ops@", "it2@", "treasury@",
}

PROJECT_ID_SUBJECT_RE = re.compile(r"\[([A-Z]{2}\d{4})\]")
SUBJECT_PROJECT_FIX_HINTS = {
    "fix",
    "uat",
    "pct",
    "connectivity test",
    "fix connectivity testing",
    "order testing",
    "test order",
    "test stock",
    "stock code",
    "production server",
    "nyse",
    "grey market",
}
