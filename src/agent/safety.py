"""
Transport-agnostic agent safety utilities.

Sanitization, fabrication detection, reasoning extraction,
message normalisation, and budget-exhaustion summary.
Shared by Telegram agent, project web agent, and support web agent.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from src.llm.client import llm_call, strip_reasoning

logger = logging.getLogger(__name__)


# ── LLM output cleaning ────────────────────────────────────────────────

_REASONING_PREFIXES = (
    "According to my instructions",
    "As an AI assistant",
    "As an AI,",
    "The user is asking me",
    "The user is greeting me",
    "The user wants me",
    "Looking through the context",
    "Looking through the knowledge",
    "I should respond",
    "I should answer",
    "I should check",
    "I should use",
    "Since this is a greeting",
    "Since this is a simple",
    "Based on the Knowledge Context",
)


def extract_fallback_text(raw: str | dict | None) -> str | None:
    """Extract human-readable text from a JSON tool result string."""
    if not raw:
        return None
    if isinstance(raw, dict):
        if "text" in raw:
            return str(raw["text"])
        keys = list(raw.keys())[:3]
        return "; ".join(f"{k}: {raw[k]}" for k in keys)
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict) and "text" in parsed:
            return str(parsed["text"])
        if isinstance(parsed, dict):
            keys = list(parsed.keys())[:3]
            return "; ".join(f"{k}: {parsed[k]}" for k in keys)
    except (json.JSONDecodeError, TypeError):
        pass
    return raw


_FABRICATION_RE = re.compile(
    r"(?:execut(?:ing|ed)\s+(?:update|tool|query))"
    r"|(?:update\s+(?:successful|completed|applied|confirmed))"
    r"|(?:successfully\s+updated)"
    r"|(?:changes?\s+(?:have been|has been)\s+(?:applied|saved|committed))"
    r"|(?:field[s]?\s+(?:updated|changed)\s+to)"
    r"|(?:note\s+recorded\s+successfully)"
    r"|(?:I'?ll\s+(?:update|add|change|set|record)\s+(?:the\s+)?(?:project|WS[-\s]?\d))"
    r"|(?:calling\s+(?:update_project|add_note|manage_followup|add_contact))"
    r"|(?:has\s+been\s+updated\s+to)"
    r"|(?:is\s+now\s+set\s+to)"
    r"|(?:now\s+set\s+to)"
    r"|(?:officially\s+clos(?:ed|ing))"
    r"|(?:project\s+is\s+(?:now\s+)?clos(?:ed|ing))"
    r"|(?:marked\s+as\s+(?:completed|closed|archived|cancelled))"
    r"|(?:moved\s+to\s+(?:M[1-8]|milestone))"
    r"|(?:advanced\s+to\s+(?:M[1-8]|milestone))"
    # P1-8: Chinese intent-style fabrication — claiming to update/close
    # without having called a write tool. (English claims are caught above.)
    r"|(?:我會幫你(?:更新|推進|改|記錄|結案))"
    r"|(?:我幫你(?:更新|推進|改|記錄|結案))"
    r"|(?:已幫你(?:更新|推進|改|結案))"
    r"|(?:會幫你(?:更新|推進|改))",
    re.IGNORECASE,
)


def extract_reasoning(text: str) -> str:
    """Extract content from <think>...</think> tags."""
    match = re.search(r"<think>(.*?)</think>", text, flags=re.DOTALL)
    if match:
        return match.group(1).strip()
    return ""


def format_reasoning_blockquote(reasoning: str) -> str:
    """Format reasoning as a blockquote with prefix."""
    lines = [ln for ln in reasoning.strip().splitlines() if ln.strip()]
    if len(lines) > 3:
        lines = lines[:2]
        lines.append("...")
    quoted = "\n".join(f"> {line}" for line in lines)
    return f"\U0001f4ad Reasoning\n{quoted}"


_LATEX_ARROWS: tuple[tuple[str, str], ...] = (
    ("Leftrightarrow", "⇔"),
    ("leftrightarrow", "↔"),
    ("Rightarrow", "⇒"),
    ("Leftarrow", "⇐"),
    ("rightarrow", "→"),
    ("leftarrow", "←"),
)
_LATEX_DOLLAR_ARROW_RE = re.compile(
    r"\$\\?(?:"
    + "|".join(cmd for cmd, _ in _LATEX_ARROWS)
    + r")\$"
)
_LATEX_BARE_ARROW_RE = re.compile(
    r"\\(?:"
    + "|".join(cmd for cmd, _ in _LATEX_ARROWS)
    + r")\b"
)


def normalize_latex_for_display(text: str) -> str:
    """Replace LaTeX arrow markup with Unicode arrows for Telegram/plain text."""
    if not text:
        return text

    def _dollar_arrow(match: re.Match[str]) -> str:
        body = match.group(0).strip("$").lstrip("\\")
        for cmd, arrow in _LATEX_ARROWS:
            if body == cmd:
                return arrow
        return match.group(0)

    def _bare_arrow(match: re.Match[str]) -> str:
        body = match.group(0).lstrip("\\")
        for cmd, arrow in _LATEX_ARROWS:
            if body == cmd:
                return arrow
        return match.group(0)

    text = _LATEX_DOLLAR_ARROW_RE.sub(_dollar_arrow, text)
    text = _LATEX_BARE_ARROW_RE.sub(_bare_arrow, text)
    return text


def sanitize_for_user(
    text: str,
    *,
    tools_were_called: bool = False,
    show_reasoning: bool = True,
) -> str:
    """Clean LLM output: strip reasoning, guard fabrication.

    Reasoning from ``<think>`` tags is surfaced as a quoted thinking block
    at the top of the response when *show_reasoning* is True.

    When *tools_were_called* is True the fabrication guard is skipped because
    the model genuinely executed tools in earlier rounds.
    """
    reasoning = extract_reasoning(text)
    text = strip_reasoning(text)
    text = re.sub(r"</think>", "", text)
    text = re.sub(r"<\|channel\b[^>]*>.*?<channel\|>", "", text, flags=re.DOTALL)
    text = re.sub(r"<\|channel\b[^>]*>.*", "", text, flags=re.DOTALL)
    lines = text.splitlines()
    filtered = [
        line for line in lines
        if not line.strip().startswith(_REASONING_PREFIXES)
    ]
    text = "\n".join(filtered).strip()
    text = normalize_latex_for_display(text)

    if not tools_were_called and text and _FABRICATION_RE.search(text):
        logger.warning("Fabrication detected in LLM output")
        return (
            "⚠️ 回覆描述咗操作，但未實際執行任何更新。"
            "請重新發送請求，等我用正確工具處理。"
        )

    if reasoning and text and show_reasoning:
        if not tools_were_called and len(text) <= 10:
            pass  # Skip reasoning for very short answers (e.g. "Yes", "OK")
        else:
            text = format_reasoning_blockquote(reasoning) + "\n\n" + text
    return text


def normalize_assistant_message(message: dict[str, Any]) -> dict[str, Any]:
    """Keep assistant tool-call messages in replayable chat format."""
    normalized: dict[str, Any] = {
        "role": message.get("role", "assistant"),
        "content": message.get("content") or "",
    }
    if message.get("tool_calls"):
        normalized["tool_calls"] = message["tool_calls"]
    return normalized


# ── Draft fabrication detection ─────────────────────────────────────────

_DRAFT_SIGNALS = re.compile(
    r"(^Subject:\s*.+|^Dear\s+\w+|^Best regards|^Kind regards|^Ready to send)",
    re.IGNORECASE | re.MULTILINE,
)


def looks_like_fabricated_draft(text: str) -> bool:
    """Return True if the LLM response looks like it contains an inline email draft."""
    hits = len(_DRAFT_SIGNALS.findall(text))
    return hits >= 2


# ── Budget exhaustion summary ───────────────────────────────────────────

def summarise_on_exhaustion(messages: list[dict]) -> str | None:
    """Make one final stripped LLM call to summarise what was accomplished.

    Used when the round budget is fully exhausted. Returns None on failure
    so callers can fall back to the last tool result.
    """
    summary_messages: list[dict[str, Any]] = []
    system = next((m for m in messages if m.get("role") == "system"), None)
    if system:
        summary_messages.append({
            "role": "system",
            "content": "You are a helpful assistant. Summarise what you just accomplished in 2-3 sentences.",
        })
    recent = [m for m in messages if m.get("role") not in ("system", "tool")][-4:]
    summary_messages.extend(recent)
    summary_messages.append({
        "role": "user",
        "content": "You have reached your processing limit. Summarise in 2-3 sentences what you accomplished and what remains.",
    })
    try:
        return llm_call("summary", summary_messages, temperature=0.0)
    except Exception:
        logger.debug("summarise_on_exhaustion: fallback LLM call failed", exc_info=True)
        return None
