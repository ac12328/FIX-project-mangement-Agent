"""Utilities for extracting the newest authored portion of an email body."""

from __future__ import annotations

import re

_QUOTE_START_PATTERNS = (
    re.compile(r"^\s*-{2,}\s*original message\s*-{2,}\s*$", re.I),
    re.compile(r"^\s*on .+ wrote:\s*$", re.I),
    re.compile(r"^\s*from:\s*.+$", re.I),
)
_OUTLOOK_HEADER = re.compile(r"^\s*(sent|to|cc|subject):\s*", re.I)
_EXTERNAL_BANNER = re.compile(
    r"(external (?:email|mail)|caution:.*external|this email originated from outside)",
    re.I,
)
_DISCLAIMER = re.compile(
    r"(confidentiality notice|this (?:email|message).*intended only|"
    r"if you are not the intended recipient|please consider the environment)",
    re.I,
)
_GREETING = re.compile(r"^\s*(hi|hello|dear)\b[^\n]{0,80}[,:]?\s*$", re.I)
_SIGNATURE = re.compile(
    r"^\s*(best regards|kind regards|regards|thanks|thank you|yours sincerely|sincerely)[,!]?\s*$",
    re.I,
)


def clean_email_body(body: str | None) -> str:
    """Return the substantive top-level authored text from an email body.

    The cleaner intentionally favors precision over aggressive HTML/content
    rewriting. It removes common transport banners, greetings, signatures,
    disclaimers, and quoted history while preserving the sender's newest
    operational update.
    """
    if not body:
        return ""

    lines = str(body).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    kept: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if any(pattern.match(line) for pattern in _QUOTE_START_PATTERNS):
            if _QUOTE_START_PATTERNS[2].match(line):
                following = lines[index + 1 : index + 5]
                if not any(_OUTLOOK_HEADER.match(item.strip()) for item in following):
                    kept.append(line)
                    index += 1
                    continue
            break
        if line.startswith(">"):
            index += 1
            continue
        if _EXTERNAL_BANNER.search(line):
            index += 1
            continue
        if _DISCLAIMER.search(line):
            break
        if _SIGNATURE.match(line) and any(item.strip() for item in kept):
            break
        kept.append(line)
        index += 1

    while kept and not kept[0].strip():
        kept.pop(0)
    if kept and _GREETING.match(kept[0]):
        kept.pop(0)
    while kept and not kept[0].strip():
        kept.pop(0)
    while kept and not kept[-1].strip():
        kept.pop()

    compact: list[str] = []
    previous_blank = False
    for line in kept:
        blank = not line.strip()
        if blank and previous_blank:
            continue
        compact.append(line.strip())
        previous_blank = blank
    return "\n".join(compact).strip()
