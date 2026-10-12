"""
Transport-agnostic intent classification utilities.

Detect whether a user message is casual, read-only (query), or a general
action request. Also: project ID extraction, portfolio detection,
and complexity heuristics.

Shared by Telegram agent and web agents.
"""
from __future__ import annotations

import re

# ── Casual message detection ────────────────────────────────────────────

_CASUAL_PATTERNS = re.compile(
    r"^("
    r"h[ie]|hi+|hey|hello|yo|sup|hola|nihao|你好|嗨|哈[嘍囉]|早[安晨]|晚安"
    r"|good\s*(morning|afternoon|evening|night|day)"
    r"|how\s*are\s*you|how'?s?\s*it\s*going|what'?s?\s*up|how\s*do\s*you\s*do"
    r"|bye|goodbye|see\s*you|later|再見|拜拜"
    r"|test|testing"
    r")[.!?\s]*$",
    re.IGNORECASE,
)

# Meta / identity chat must not inherit sticky project_id for activity attribution.
_META_PATTERNS = re.compile(
    r"("
    r"who\s+are\s+you"
    r"|what\s+are\s+you"
    r"|what\s+model"
    r"|which\s+model"
    r"|your\s+(name|model|identity)"
    r"|你是誰|你係邊個|你是什麼|你係咩"
    r"|你用咩|你用什麼|底層模型|什麼模型|咩模型"
    r"|介紹一下你|自我介紹"
    r")",
    re.IGNORECASE,
)


def is_casual_message(text: str) -> bool:
    """Return True if the message is a greeting, acknowledgement, or small talk."""
    stripped = text.strip()
    return bool(_CASUAL_PATTERNS.match(stripped)) or bool(_META_PATTERNS.search(stripped))


# ── Portfolio detection ─────────────────────────────────────────────────

_PORTFOLIO_PATTERNS = re.compile(
    r"("
    r"\bprojects\b"
    r"|all\s+(the\s+)?projects?"
    r"|among\s+all"
    r"|every\s+project"
    r"|portfolio"
    r"|across\s+projects?"
    r"|所有(的)?(?:專案|項目)"
    r"|全部(?:專案|項目)"
    r"|(?:各個|每個|目前|現有|整體|全體).{0,8}(?:專案|項目)"
    r"|(?:專案|項目).{0,8}(?:清單|列表|總覽|概況|最新進度|最新更新|進展)"
    r"|(?:\u5572|\u54ea\u4e9b|\u908a\u5572)\s*(?:\u5c08\u6848|\u9805\u76ee)"
    r"|each\s+project"
    r"|邊個\s*project"
    r"|which\s+project"
    r"|ball\s*with\s+(external|internal|shared)"
    r"|超過\s*\d+\s*日"
    r"|\u5187\s*update|\u672a\u66f4\u65b0|no\s+update"
    r"|今日.*(跟進|brief|晨報)"
    r"|有咩.*跟進"
    r"|list\s+work\s+items?"
    r"|work\s+items?"
    # Multi-project "what's outstanding / blocked" (not a single-project status)
    r"|any\s+projects?\s+outstanding"
    r"|what(?:'s|\s+is)?\s+outstanding\s+projects?"
    r"|outstanding\s+projects?"
    r"|projects?\s+outstanding"
    r"|blocked\s+projects?"
    r"|waiting\s+projects?"
    r"|\u54ea\u4e9b\u9805\u76ee|\u6709\u908a\u5572\u9805\u76ee|\u5361\u4f4f.*\u9805\u76ee"
    r")",
    re.IGNORECASE,
)


def is_portfolio_query(text: str) -> bool:
    """Return True if the message is about multiple/all projects."""
    return bool(_PORTFOLIO_PATTERNS.search(text.strip()))


# ── Ad-hoc email (no project / ticket) ──────────────────────────────────

_EMAIL_ADDR_RE = re.compile(
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}",
)

# Operator can say "ad hoc email" / "adhoc email" as the explicit mode keyword.
_ADHOC_EXPLICIT_RE = re.compile(
    r"(?:"
    r"\bad[-\s]?hoc\s+e-?mails?\b"
    r"|\badhoc\s+e-?mails?\b"
    r"|\bad[-\s]?hoc\s+(?:draft|send|mail)\b"
    r"|\badhoc\s+(?:draft|send|mail)\b"
    r"|\bstandalone\s+e-?mails?\b"
    r"|獨立郵件|臨時郵件|ad\s*hoc\s*郵件|adhoc\s*郵件"
    r")",
    re.IGNORECASE,
)

_ADHOC_COMPOSE_RE = re.compile(
    r"(?:"
    r"\b(?:send|email|mail|forward|draft|compose|write)\b"
    r"|寄|發送|發給|送俾|寫信|草擬|起草"
    r")",
    re.IGNORECASE,
)

# Deictic / project-bound language — sticky project still applies.
_PROJECT_DEICTIC_RE = re.compile(
    r"(?:"
    r"\bthis\s+project\b|\bthe\s+project\b|\bthat\s+project\b"
    r"|\bfor\s+(?:the\s+)?(?:client|case)\b"
    r"|呢個項目|呢單|呢個 case|該項目|本項目"
    r"|support\s+ticket|\bticket\b"
    r")",
    re.IGNORECASE,
)

_ADHOC_HINT_RE = re.compile(
    r"(?:"
    r"\bcolleague\b|\bcoworker\b|\binternal\b"
    r"|no\s+project|not\s+related\s+to\s+(?:any\s+)?project"
    r"|new\s+(?:request|event|email)|adhoc|ad-hoc|ad\s+hoc|standalone"
    r"|唔關.*project|無關.*項目|新事件|新 request"
    r"|my\s+colleague|同事"
    r")",
    re.IGNORECASE,
)


def is_adhoc_email_request(text: str) -> bool:
    """True when operator wants an email not bound to a project or ticket.

    Preferred operator phrase: **\"ad hoc email\"** / **\"adhoc email\"**
    (optionally with To address and body instructions).

    Also: compose/send + recipient email, no project ID, colleague/new-event
    hints. Sticky active project must not capture these turns.
    """
    raw = (text or "").strip()
    if not raw:
        return False
    if detect_project_id(raw):
        return False

    # Explicit mode keyword — enough to skip sticky even before To is named.
    if _ADHOC_EXPLICIT_RE.search(raw):
        return True

    if not _EMAIL_ADDR_RE.search(raw):
        return False
    if not _ADHOC_COMPOSE_RE.search(raw):
        return False
    if _PROJECT_DEICTIC_RE.search(raw) and not _ADHOC_HINT_RE.search(raw):
        return False
    return True


# ── Project ID extraction ──────────────────────────────────────────────

def detect_project_id(text: str) -> str | None:
    """Extract an explicit project ID from the user message (no DB lookup)."""
    from src.agent.project_resolve import resolve_project_reference

    result = resolve_project_reference(text)
    if result.ambiguous or not result.project_id:
        return None
    return result.project_id


def detect_project_id_from_db(conn, text: str) -> str | None:
    """Resolve project ID using known portfolio IDs from the database."""
    from src.agent.project_resolve import resolved_project_id

    return resolved_project_id(conn, text)


# ── Complexity detection ────────────────────────────────────────────────

def is_complex_request(text: str) -> bool:
    t = text.lower()
    indicators = [
        len(text) > 150,
        text.count(" and ") >= 2,
        "then" in t,
        "all" in t and "project" in t,
        bool(re.search(r"\b\d+\s+project", text, re.IGNORECASE)),
        "compare" in t,
        "review" in t and "portfolio" in t,
        ("update" in t or bool(re.search(r"\bm[1-8]\b", t))) and ("close" in t or "production" in t),
        "close" in t and ("update" in t or "milestone" in t or "status" in t),
    ]
    return sum(indicators) >= 2


# ── Intent classification ───────────────────────────────────────────────

_QUERY_KEYWORDS = frozenset({
    "status", "show", "list", "find", "search", "who", "what", "when",
    "how many", "how is", "where", "which", "display",
    "/status", "/query", "/portfolio", "/pending",
})

_ACTION_KEYWORDS = frozenset({
    "update", "change", "set", "advance", "move", "add", "note", "log",
    "record", "draft", "send", "create", "close", "reopen", "flag",
    "follow", "track", "approve", "reject", "edit", "fix", "resolve",
    "assign", "remove", "delete", "cancel",
})


def classify_intent(user_text: str) -> str:
    """Classify the user message into 'casual', 'query', 'email', or 'general'.

    Conservative: defaults to 'general' (full tool bundle) unless the message is
    clearly read-only or an inbound email. Use :func:`resolve_tool_intent` to map
    ``general`` → ``orchestrator`` when the host runs in orchestrator mode.

    Legacy tool routing only — when ``unified_loop_v2`` is enabled, use
    :func:`label_intent` for telemetry/context and :func:`prepare_loop` with
    ``unified_v2=True`` for harness policy (role-based tools).
    """
    if not user_text:
        return "general"

    if user_text.startswith("[INBOUND EMAIL]") or user_text.startswith("[INBOUND REPLY]"):
        return "email"

    lower = user_text.lower()

    if any(kw in lower for kw in _ACTION_KEYWORDS):
        return "general"

    if is_casual_message(user_text):
        # Never treat project-specific messages as casual greetings
        if detect_project_id(user_text):
            return "general"
        return "casual"

    if len(user_text.split()) <= 3:
        return "general"

    if any(kw in lower for kw in _QUERY_KEYWORDS):
        return "query"

    return "general"


def label_intent(user_text: str) -> str:
    """Telemetry/context label only — does not select tool bundles in unified v2."""
    return classify_intent(user_text)


def resolve_tool_intent(base_intent: str, *, orchestrator_mode: bool = False) -> str:
    """Map :func:`classify_intent` output to the tool-bundle intent.

    Default Telegram host chat uses the thin orchestrator bundle when
    ``orchestrator_mode`` is enabled. Email, casual, and query intents are
    unchanged — they keep their dedicated toolsets.
    """
    if orchestrator_mode and base_intent == "general":
        return "orchestrator"
    return base_intent
