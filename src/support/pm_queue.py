"""PM desk-queue derivation for support tickets.

Display-only helpers. Nothing here writes ticket fields.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from src.models.enums import BallWith

ACTIVE_TICKET_STATUSES = (
    "open",
    "investigating",
    "waiting_external",
    "waiting_internal",
)
TERMINAL_TICKET_STATUSES = frozenset({"resolved", "closed"})
JUNK_PROJECT_IDS = frozenset({"PROJ-001", "_system"})
AFFILIATE_DOMAINS = frozenset(
    {
        "demo.invalid",
        "acceptance.demo.invalid",
        "provisioning.demo.invalid",
        "exception.demo.invalid",
    }
)

_ACCOUNT_RE = re.compile(r"\bP\d{5,7}[A-Z]?\b", re.IGNORECASE)
_PREFIXES = (
    "re:",
    "fw:",
    "fwd:",
    "re：",
    "fw：",
    "回复:",
    "回复：",
    "回覆:",
    "回覆：",
    "答复:",
    "答复：",
    "答覆:",
    "答覆：",
    "转发:",
    "转发：",
    "轉發:",
    "轉發：",
)
_BRACKET_TOKENS = (
    "[bulk]",
    "[external email]",
    "[external]",
    "[外部郵件]",
    "[warning: message encrypted]",
)
_DISCLAIMERS = (
    "EXTERNAL EMAIL",
    "外部郵件",
    "Please do not click any link",
    "除非您信任寄件者",
)
_ANSWERED_MARKERS = (
    "noted with thanks",
    "password reset",
    "has been rebooked",
    "please try again",
    "already fixed",
    "already amended",
    "amended bcan",
    "please kindly noted",
    "pls noted",
    "kindly noted",
    "no further",
    "problem solved",
    "已修正",
    "已經修正",
    "done，thanks",
    "done, thanks",
    "done thanks",
)
_HANDOFF_MARKERS = (
    "please check",
    "pls follow up",
    "pls help",
    "please help",
    "please assist",
    "請協助",
    "請跟進",
)
_NOISE_SUBJECTS = (
    "newsflash",
    "syncing paused",
    "payment issue",
    "help desk inquiry",
    "250gb storage",
    "provisioning hub sequence reset",
)
# Independently authored noise examples; no real sender roster.
_NOISE_SENDERS = ("newsletter@noise.demo.invalid",)

_INCIDENT_MARKERS = (
    "connection is closed",
    "connection closed",
    "session down",
    "disconnect",
    "vpn",
    "firewall",
    "streaming",
    "timeout",
    "network broken",
    "certificat",
    "credential expiration",
    "alert",
    "outage",
)
_TRADE_MARKERS = (
    "order reject",
    "order rejected",
    "被拒绝",
    "被駁回",
    "not filled",
    "amendment",
    "cancel failure",
    "execution report",
    "trade confo",
    "bcan",
    "超賣",
    "未成交",
    "fill",
    "clordid",
)
_ACCESS_MARKERS = (
    "password",
    "2fa",
    "unlock",
    "login",
    "suspended",
    "unbind",
    "reset password",
    "user permission",
    "white label",
)
_PROJECT_MARKERS = (
    "uat",
    "testing",
    "onboarding",
    "new session",
    "ip/port",
    "kick off",
    "fix project",
    "連線測試",
    "application testing",
    "grey market fix",
    "gray market fix",
)


@dataclass(frozen=True)
class TicketQueueInput:
    ticket_id: str
    title: str
    category: str = "other"
    severity: str = "medium"
    status: str = "open"
    description: str | None = None
    related_project_id: str | None = None
    counterparty: str | None = None
    party_name: str | None = None
    reporter_email: str | None = None
    last_sender: str | None = None
    last_subject: str | None = None
    last_preview: str | None = None
    last_activity_at: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True)
class TicketQueueView:
    display_title: str
    party_name: str | None
    reporter_email: str | None
    account_hint: str | None
    case_type: str
    queue: str
    next_action: str
    ball_with: str
    last_activity_at: str | None
    last_sender: str | None
    last_subject: str | None
    last_preview: str | None
    age_days: int | None
    related_project_name: str | None
    on_project: bool
    is_noise: bool
    already_answered: bool
    mixed_thread: bool


def internal_email_domains() -> set[str]:
    try:
        from config.settings import CONFIG

        configured = {str(domain).lower() for domain in getattr(CONFIG, "company_domains", set())}
    except Exception:
        configured = set()
    return configured | set(AFFILIATE_DOMAINS)


def sender_domain(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    return email.rsplit("@", 1)[1].strip().lower() or None


def is_internal_email(email: str | None) -> bool:
    domain = sender_domain(email)
    return bool(domain and domain in internal_email_domains())


def clean_display_title(title: str | None) -> str:
    text = (title or "").strip()
    changed = True
    while changed and text:
        changed = False
        lowered = text.lower()
        for prefix in _PREFIXES:
            if lowered.startswith(prefix):
                text = text[len(prefix) :].strip()
                changed = True
                break
        lowered = text.lower()
        for token in _BRACKET_TOKENS:
            if lowered.startswith(token):
                text = text[len(token) :].strip()
                changed = True
                break
    return re.sub(r"\s+", " ", text).strip() or (title or "").strip()


def strip_disclaimer(text: str | None) -> str:
    raw = (text or "").replace("\r\n", "\n")
    if not raw.strip():
        return ""
    for marker in _DISCLAIMERS:
        idx = raw.find(marker)
        if idx == -1:
            continue
        newline = raw.find("\n", idx)
        if newline != -1:
            raw = raw[newline + 1 :]
    cleaned = re.sub(r"\[cid:[^\]]+\]", " ", raw)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def extract_account_hint(*texts: str | None) -> str | None:
    found: list[str] = []
    seen: set[str] = set()
    for text in texts:
        for match in _ACCOUNT_RE.findall(text or ""):
            token = match.upper()
            if token not in seen:
                seen.add(token)
                found.append(token)
    if not found:
        return None
    return " / ".join(found[:3])


def subject_family(value: str | None) -> str:
    cleaned = clean_display_title(value).lower()
    cleaned = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def is_mixed_thread(title: str | None, last_subject: str | None) -> bool:
    left = subject_family(title)
    right = subject_family(last_subject)
    if not left or not right or left == right:
        return False
    if left in right or right in left:
        return False
    left_tokens = set(left.split())
    right_tokens = set(right.split())
    if not left_tokens or not right_tokens:
        return False
    overlap = len(left_tokens & right_tokens) / min(len(left_tokens), len(right_tokens))
    return overlap < 0.4


def _haystack(*parts: str | None) -> str:
    return " ".join(part for part in parts if part).lower()


def is_noise_ticket(
    *,
    title: str | None = None,
    description: str | None = None,
    related_project_id: str | None = None,
    reporter_email: str | None = None,
    last_subject: str | None = None,
) -> bool:
    if (related_project_id or "") in JUNK_PROJECT_IDS:
        return True
    sender = (reporter_email or "").lower()
    if any(marker in sender for marker in _NOISE_SENDERS):
        return True
    text = _haystack(title, description, last_subject)
    return any(marker in text for marker in _NOISE_SUBJECTS)


def is_noise_inbound(subject: str | None, sender_email: str | None, body: str | None = None) -> bool:
    return is_noise_ticket(title=subject, description=body, reporter_email=sender_email)


def infer_case_type(
    *,
    title: str | None,
    description: str | None = None,
    category: str | None = None,
    related_project_id: str | None = None,
    last_subject: str | None = None,
    reporter_email: str | None = None,
) -> str:
    if is_noise_ticket(
        title=title,
        description=description,
        related_project_id=related_project_id,
        reporter_email=reporter_email,
        last_subject=last_subject,
    ):
        return "noise"
    text = _haystack(title, description, last_subject, category)
    if any(marker in text for marker in _INCIDENT_MARKERS) or category in {"fix_session", "network", "cert", "monitoring"}:
        return "incident"
    if any(marker in text for marker in _TRADE_MARKERS) or category in {"trade", "post_trade"}:
        return "trade"
    if any(marker in text for marker in _ACCESS_MARKERS) or category == "access":
        return "access"
    if any(marker in text for marker in _PROJECT_MARKERS):
        return "project"
    if related_project_id and related_project_id not in JUNK_PROJECT_IDS:
        return "project"
    return "other"


def already_answered(*, last_sender: str | None, last_preview: str | None, last_subject: str | None = None) -> bool:
    text = _haystack(last_preview, last_subject)
    if not text:
        return False
    if any(marker in text for marker in _ANSWERED_MARKERS):
        return True
    if is_internal_email(last_sender) and re.search(r"\bdone\b", text) and "please" not in text:
        return True
    return False


def resolve_party_name(
    *,
    party_name: str | None,
    counterparty: str | None,
    reporter_email: str | None,
    last_sender: str | None = None,
) -> str | None:
    for candidate in (party_name, counterparty):
        if candidate and candidate.strip() and candidate not in JUNK_PROJECT_IDS:
            return candidate.strip()
    for email in (reporter_email, last_sender):
        if is_internal_email(email):
            continue
        domain = sender_domain(email)
        if domain:
            return domain
    return None


def derive_ball_from_last_sender(last_sender: str | None, status: str | None = None) -> str:
    if status in TERMINAL_TICKET_STATUSES:
        return BallWith.UNKNOWN.value
    if status == "waiting_external":
        return BallWith.EXTERNAL.value
    if status == "waiting_internal":
        return BallWith.INTERNAL.value
    if not last_sender:
        return BallWith.INTERNAL.value
    if is_internal_email(last_sender):
        return BallWith.EXTERNAL.value
    return BallWith.INTERNAL.value


def _parse_when(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def age_days(value: str | None, *, now: datetime | None = None) -> int | None:
    parsed = _parse_when(value)
    if parsed is None:
        return None
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return max(0, (current - parsed).days)


def classify_queue(
    *,
    status: str,
    is_noise: bool,
    ball_with: str,
    age: int | None,
    last_sender: str | None,
) -> str:
    if status in TERMINAL_TICKET_STATUSES:
        return "noise" if is_noise else "stale"
    if is_noise:
        return "noise"
    last_internal = is_internal_email(last_sender)
    if age is not None and (age >= 14 or (last_internal and age >= 7)):
        return "stale"
    if ball_with == BallWith.EXTERNAL.value:
        return "waiting"
    return "needs_me"


def classify_next_action(
    *,
    queue: str,
    already: bool,
    on_project: bool,
    case_type: str,
    mixed_thread: bool,
) -> str:
    if queue == "noise":
        return "review"
    if already or queue == "stale":
        return "close_stale"
    if queue == "waiting":
        return "follow_up"
    if not on_project and case_type == "project":
        return "link_project"
    if mixed_thread:
        return "review"
    return "reply"


def derive_ticket_queue(
    item: TicketQueueInput,
    *,
    related_project_name: str | None = None,
    now: datetime | None = None,
) -> TicketQueueView:
    display_title = clean_display_title(item.title)
    last_preview = strip_disclaimer(item.last_preview) or strip_disclaimer(item.description)
    noise = is_noise_ticket(
        title=item.title,
        description=item.description,
        related_project_id=item.related_project_id,
        reporter_email=item.reporter_email,
        last_subject=item.last_subject,
    )
    case_type = infer_case_type(
        title=item.title,
        description=item.description,
        category=item.category,
        related_project_id=item.related_project_id,
        last_subject=item.last_subject,
        reporter_email=item.reporter_email,
    )
    party = resolve_party_name(
        party_name=item.party_name,
        counterparty=item.counterparty,
        reporter_email=item.reporter_email,
        last_sender=item.last_sender,
    )
    activity_at = item.last_activity_at or item.updated_at
    age = age_days(activity_at, now=now)
    ball = derive_ball_from_last_sender(item.last_sender, item.status)
    answered = already_answered(
        last_sender=item.last_sender,
        last_preview=last_preview,
        last_subject=item.last_subject,
    )
    mixed = is_mixed_thread(item.title, item.last_subject)
    on_project = bool(item.related_project_id) and item.related_project_id not in JUNK_PROJECT_IDS
    queue = classify_queue(
        status=item.status,
        is_noise=noise,
        ball_with=ball,
        age=age,
        last_sender=item.last_sender,
    )
    return TicketQueueView(
        display_title=display_title,
        party_name=party,
        reporter_email=item.reporter_email,
        account_hint=extract_account_hint(item.title, item.description, item.last_subject, item.last_preview),
        case_type=case_type,
        queue=queue,
        next_action=classify_next_action(
            queue=queue,
            already=answered,
            on_project=on_project,
            case_type=case_type,
            mixed_thread=mixed,
        ),
        ball_with=ball,
        last_activity_at=activity_at,
        last_sender=item.last_sender,
        last_subject=item.last_subject,
        last_preview=(last_preview[:180] if last_preview else None),
        age_days=age,
        related_project_name=related_project_name or None,
        on_project=on_project,
        is_noise=noise,
        already_answered=answered,
        mixed_thread=mixed,
    )


def queue_counts(views: Iterable[tuple[str, TicketQueueView]]) -> dict[str, int]:
    counts = {
        "needs_me": 0,
        "waiting": 0,
        "stale": 0,
        "noise": 0,
        "on_project": 0,
        "all_active": 0,
    }
    for status, view in views:
        if status in TERMINAL_TICKET_STATUSES:
            continue
        counts["all_active"] += 1
        counts[view.queue] = counts.get(view.queue, 0) + 1
        if view.on_project:
            counts["on_project"] += 1
    return counts
