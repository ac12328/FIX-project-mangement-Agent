"""Text helpers for Telegram streaming messages."""

from __future__ import annotations

import html
import re

from src.agent.safety import normalize_latex_for_display

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_MEDIA_RE = re.compile(r"MEDIA:\s*\S+", re.IGNORECASE)
_MDV2_ESCAPE_RE = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")
_FENCE_RE = re.compile(r"```(\w+)?\n([\s\S]*?)```")
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_CODE_RE = re.compile(r"`([^`]+)`")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")
_UL_RE = re.compile(r"^\s*[-*+]\s+(.+)$")
_OL_RE = re.compile(r"^\s*\d+\.\s+(.+)$")
_QUOTE_RE = re.compile(r"^>\s?(.*)$")
_TABLE_SEPARATOR_RE = re.compile(
    r"^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*){1,}\|?\s*$"
)


def utf16_len(text: str) -> int:
    """Telegram counts regular message length in UTF-16 code units."""
    return len(text.encode("utf-16-le")) // 2


def char_len(text: str) -> int:
    """Unicode code-point length (Rich Message ~32k character budget)."""
    return len(text)


def clean_for_display(text: str) -> str:
    """Strip hidden reasoning / media tags before showing to the user."""
    text = _THINK_RE.sub("", text)
    text = _MEDIA_RE.sub("", text)
    text = normalize_latex_for_display(text)
    return text.strip()


def sanitize_llm_markdown(text: str) -> str:
    """Prepare LLM GFM for Rich Markdown (or downstream HTML conversion)."""
    return clean_for_display(text)


def escape_markdown_v2(text: str) -> str:
    return _MDV2_ESCAPE_RE.sub(r"\\\1", text)


def escape_html(text: str) -> str:
    return html.escape(text, quote=False)


def _inline_md_to_html(text: str) -> str:
    s = escape_html(text)
    s = _LINK_RE.sub(r'<a href="\2">\1</a>', s)
    s = _BOLD_RE.sub(r"<b>\1</b>", s)
    s = _CODE_RE.sub(r"<code>\1</code>", s)
    return s


def _split_table_row(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _render_table_block(lines: list[str]) -> str:
    if len(lines) < 3:
        return "\n".join(lines)

    headers = _split_table_row(lines[0])
    if len(headers) < 2:
        return "\n".join(lines)

    rendered: list[str] = []
    for index, line in enumerate(lines[2:], start=1):
        cells = _split_table_row(line)
        if len(cells) < len(headers):
            cells.extend([""] * (len(headers) - len(cells)))
        elif len(cells) > len(headers):
            cells = cells[: len(headers)]

        heading_index = next((i for i, value in enumerate(cells) if value), None)
        if heading_index is None:
            heading = f"Row {index}"
        else:
            heading = cells[heading_index]
        bold_heading = re.fullmatch(r"\*\*(.+)\*\*", heading)
        if bold_heading:
            heading = bold_heading.group(1)

        bullets = [
            f"• {header}: {value}"
            for cell_index, (header, value) in enumerate(zip(headers, cells))
            if header and value and cell_index != heading_index
        ]
        rendered.append("\n".join([f"**{heading}**", *bullets]))

    return "\n\n".join(rendered)


def normalize_markdown_tables_for_mobile(text: str) -> str:
    """Rewrite GFM tables as compact row blocks for Telegram mobile clients."""
    if "|" not in text or "-" not in text:
        return text

    lines = text.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    in_fence = False
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            out.append(line)
            index += 1
            continue
        if in_fence:
            out.append(line)
            index += 1
            continue

        if (
            "|" in line
            and index + 1 < len(lines)
            and _TABLE_SEPARATOR_RE.match(lines[index + 1])
        ):
            block = [line, lines[index + 1]]
            cursor = index + 2
            while cursor < len(lines) and lines[cursor].strip() and "|" in lines[cursor]:
                block.append(lines[cursor])
                cursor += 1
            out.append(_render_table_block(block))
            index = cursor
            continue

        out.append(line)
        index += 1

    return "\n".join(out)


def markdown_to_telegram_html(md: str) -> str:
    """Best-effort GFM → Telegram HTML (regular messages fallback).

    Not a full Markdown parser — enough for agent replies: headings, bold,
    lists, quotes, fenced code, links. Tables become plain lines.
    """
    text = normalize_markdown_tables_for_mobile(md.replace("\r\n", "\n"))

    fences: list[str] = []

    def _store_fence(match: re.Match[str]) -> str:
        lang = match.group(1) or ""
        code = match.group(2).rstrip("\n")
        lang_attr = f' class="language-{escape_html(lang)}"' if lang else ""
        fences.append(f"<pre><code{lang_attr}>{escape_html(code)}</code></pre>")
        return f"\n%%FENCE_{len(fences) - 1}%%\n"

    text = _FENCE_RE.sub(_store_fence, text)
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        fence_m = re.match(r"^%%FENCE_(\d+)%%$", line.strip())
        if fence_m:
            out.append(fences[int(fence_m.group(1))])
            i += 1
            continue
        if _QUOTE_RE.match(line) or line == ">":
            qs: list[str] = []
            while i < len(lines) and (_QUOTE_RE.match(lines[i]) or lines[i] == ">"):
                qs.append(_QUOTE_RE.match(lines[i]).group(1) if _QUOTE_RE.match(lines[i]) else "")
                i += 1
            out.append("<blockquote>" + "\n".join(_inline_md_to_html(q) for q in qs) + "</blockquote>")
            continue
        hm = _HEADING_RE.match(line)
        if hm:
            out.append(f"<b>{_inline_md_to_html(hm.group(2))}</b>")
            i += 1
            continue
        if line.strip() == "---" or re.fullmatch(r"-{3,}", line.strip() or ""):
            out.append("")
            i += 1
            continue
        if _UL_RE.match(line):
            items: list[str] = []
            while i < len(lines) and _UL_RE.match(lines[i]):
                items.append(f"• {_inline_md_to_html(_UL_RE.match(lines[i]).group(1))}")
                i += 1
            out.append("\n".join(items))
            continue
        if _OL_RE.match(line):
            items = []
            n = 1
            while i < len(lines) and _OL_RE.match(lines[i]):
                items.append(f"{n}. {_inline_md_to_html(_OL_RE.match(lines[i]).group(1))}")
                n += 1
                i += 1
            out.append("\n".join(items))
            continue
        if not line.strip():
            out.append("")
            i += 1
            continue
        paras = [line]
        i += 1
        while (
            i < len(lines)
            and lines[i].strip()
            and not _HEADING_RE.match(lines[i])
            and not _QUOTE_RE.match(lines[i])
            and not _UL_RE.match(lines[i])
            and not _OL_RE.match(lines[i])
            and not re.match(r"^%%FENCE_\d+%%$", lines[i].strip())
        ):
            paras.append(lines[i])
            i += 1
        out.append("\n".join(_inline_md_to_html(p) for p in paras))

    # Collapse excessive blank lines
    rendered = "\n".join(out)
    rendered = re.sub(r"\n{3,}", "\n\n", rendered).strip()
    return rendered


def truncate_message(
    text: str,
    max_len: int,
    *,
    len_fn=utf16_len,
) -> list[str]:
    """Split *text* into Telegram-safe chunks."""
    if len_fn(text) <= max_len:
        return [text]

    chunks: list[str] = []
    current = ""
    suffix_reserve = 16

    for ch in text:
        candidate = current + ch
        if len_fn(candidate) > max_len - suffix_reserve:
            if current:
                chunks.append(current)
            current = ch
        else:
            current = candidate

    if current:
        chunks.append(current)

    total = len(chunks)
    if total <= 1:
        return chunks

    numbered: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        label = f"({index}/{total})"
        budget = max_len - len_fn(label) - 2
        body = chunk
        while body and len_fn(body) > budget:
            body = body[:-1]
        numbered.append(f"{body}\n\n{label}")
    return numbered