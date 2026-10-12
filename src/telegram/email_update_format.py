"""Format email-analysis Telegram cards: structured, locale-aware, scannable."""

from __future__ import annotations

import re
from typing import Any

from src.telegram.i18n import get_ui_language, t

# Soft cap for narrative summary on mobile (D2)
SUMMARY_MAX_CHARS = 450

# Field labels by locale
FIELD_LABELS: dict[str, dict[str, str]] = {
    "zh-TW": {
        "ball_with": "持球方",
        "current_milestone": "里程碑",
        "next_action": "下一步",
        "project_status": "專案狀態",
        "notes": "備註",
        "target_go_live": "目標上線",
        "client_name": "客戶名稱",
        "client_email": "客戶電郵",
        "market": "市場",
        "broker_code": "Broker 代碼",
        "connection_type": "連線類型",
        "sla_limit": "SLA",
        "project_tier": "優先級",
    },
    "en": {
        "ball_with": "Ball with",
        "current_milestone": "Milestone",
        "next_action": "Next action",
        "project_status": "Project status",
        "notes": "Notes",
        "target_go_live": "Target go-live",
        "client_name": "Client name",
        "client_email": "Client email",
        "market": "Market",
        "broker_code": "Broker code",
        "connection_type": "Connection type",
        "sla_limit": "SLA",
        "project_tier": "Priority",
    },
}

# Backward-compatible alias used by tests
FIELD_LABELS_ZH = FIELD_LABELS["zh-TW"]

_FIX_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("TargetCompID", re.compile(r"TargetCompID\s*[=:]\s*([A-Za-z0-9_]+)", re.I)),
    ("SenderCompID", re.compile(r"SenderCompID\s*[=:]\s*([A-Za-z0-9_]+)", re.I)),
    (
        "Primary",
        re.compile(
            r"(?:Primary(?:\s+IP)?|主(?:IP)?)\s*[=:]?\s*"
            r"(\d{1,3}(?:\.\d{1,3}){3}\s*:\s*\d+)",
            re.I,
        ),
    ),
    (
        "Backup",
        re.compile(
            r"(?:Backup(?:\s+IP)?|備(?:份)?(?:IP)?)\s*[=:]?\s*"
            r"(\d{1,3}(?:\.\d{1,3}){3}\s*:\s*\d+)",
            re.I,
        ),
    ),
]


def field_label(field: str, lang: str | None = None) -> str:
    locale = lang or get_ui_language()
    labels = FIELD_LABELS.get(locale) or FIELD_LABELS["zh-TW"]
    return labels.get(field, field)


def field_label_zh(field: str) -> str:
    """Backward-compatible Chinese field label."""
    return field_label(field, "zh-TW")


def truncate_summary(text: str, max_chars: int = SUMMARY_MAX_CHARS) -> str:
    """Truncate narrative; keep whole if under limit."""
    s = (text or "").strip()
    if len(s) <= max_chars:
        return s
    cut = s[: max_chars - 1].rsplit(" ", 1)[0] if " " in s[:max_chars] else s[: max_chars - 1]
    return cut.rstrip("，。,.;；") + "…"


def extract_fix_params(*texts: str) -> dict[str, str]:
    """Pull structured FIX session params from free text (never leave only in prose)."""
    blob = "\n".join(t for t in texts if t)
    found: dict[str, str] = {}
    for label, pat in _FIX_PATTERNS:
        m = pat.search(blob)
        if m and label not in found:
            found[label] = re.sub(r"\s+", "", m.group(1))
    # Generic host:port pairs if primary/backup missed
    if "Primary" not in found:
        ips = re.findall(r"\b(\d{1,3}(?:\.\d{1,3}){3}:\d+)\b", blob)
        if ips:
            found["Primary"] = ips[0]
            if len(ips) > 1 and "Backup" not in found:
                found["Backup"] = ips[1]
    return found


def format_change_lines(
    changes: list[dict[str, Any]] | None,
    lang: str | None = None,
) -> list[str]:
    """old → new lines with locale field labels (D4)."""
    locale = lang or get_ui_language()
    lines: list[str] = []
    for ch in changes or []:
        fld = field_label(str(ch.get("field") or "?"), locale)
        old_v = ch.get("old_value")
        new_v = ch.get("new_value")
        new_s = "—" if new_v is None else str(new_v)
        if old_v is not None and str(old_v) != "":
            lines.append(f"• {fld}: {old_v} → {new_s}")
        else:
            lines.append(f"• {fld}: → {new_s}")
    return lines


def format_fix_block(params: dict[str, str], lang: str | None = None) -> list[str]:
    if not params:
        return []
    locale = lang or get_ui_language()
    header = t("email_card.fix_header", locale)
    lines = [header]
    for key in ("TargetCompID", "SenderCompID", "Primary", "Backup"):
        if key in params:
            lines.append(f"• {key}={params[key]}")
    for key, val in params.items():
        if key not in ("TargetCompID", "SenderCompID", "Primary", "Backup"):
            lines.append(f"• {key}={val}")
    return lines


def write_status_banner(status: str, lang: str | None = None) -> str:
    """D1 status line for cards."""
    locale = lang or get_ui_language()
    key = {
        "applied": "email_card.status_applied",
        "pending_review": "email_card.status_pending",
        "info_only": "email_card.status_info_only",
        "skipped": "email_card.status_skipped",
    }.get(status)
    if key:
        return t(key, locale)
    return t("email_card.status_generic", locale).format(status=status)


def build_email_card_body(
    *,
    project_id: str,
    client_name: str | None = None,
    subject: str | None = None,
    sender_email: str | None = None,
    summary: str = "",
    suggested_action: str = "",
    write_status: str = "info_only",
    proposed_changes: list[dict[str, Any]] | None = None,
    reason: str | None = None,
    confidence: float | None = None,
    extra_text: str | None = None,
    lang: str | None = None,
) -> str:
    """Full card body for email analysis (applied / pending / info)."""
    locale = lang or get_ui_language()
    title_bits = [project_id]
    if client_name:
        title_bits.append(client_name.strip())
    lines: list[str] = [
        write_status_banner(write_status, locale),
        f"📌 {' · '.join(title_bits)}",
    ]
    if subject:
        lines.append(f"✉ {(subject or '')[:100]}")
    if sender_email:
        lines.append(f"👤 {sender_email}")

    narrative_src = summary or suggested_action or extra_text or ""
    fix_params = extract_fix_params(summary, suggested_action, extra_text or "")
    narrative = truncate_summary(narrative_src)
    if narrative:
        lines.append("")
        lines.append(t("email_card.summary_header", locale))
        lines.append(narrative)

    if suggested_action and suggested_action.strip() != narrative_src.strip():
        lines.extend(["", "Suggested next step" if locale == "en" else "建議下一步",
                      truncate_summary(suggested_action)])

    fix_lines = format_fix_block(fix_params, locale)
    if fix_lines:
        lines.append("")
        lines.extend(fix_lines)

    change_lines = format_change_lines(proposed_changes, locale)
    if change_lines:
        lines.append("")
        if write_status == "applied":
            lines.append(t("email_card.fields_applied", locale))
        elif write_status == "pending_review":
            lines.append(t("email_card.fields_pending", locale))
        else:
            lines.append(t("email_card.fields_header", locale))
        lines.extend(change_lines)

    if reason or confidence is not None:
        lines.append("")
        bits = []
        if reason:
            bits.append(reason)
        if confidence is not None:
            bits.append(t("email_card.confidence", locale).format(pct=f"{confidence:.0%}"))
        lines.append("💡 " + " · ".join(bits))

    if write_status == "applied":
        lines.append("")
        lines.append(t("email_card.footer_applied", locale))
    elif write_status == "pending_review":
        lines.append("")
        lines.append(t("email_card.footer_pending", locale))

    return "\n".join(lines)


def build_email_card_title(write_status: str, project_id: str, lang: str | None = None) -> str:
    locale = lang or get_ui_language()
    if write_status == "applied":
        return t("email_card.title_applied", locale).format(project_id=project_id)
    if write_status == "pending_review":
        return t("email_card.title_pending", locale)
    return t("email_card.title_info", locale).format(project_id=project_id)
