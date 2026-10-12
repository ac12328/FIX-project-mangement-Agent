"""Strict HTML sanitizing and plain-text conversion for email drafts."""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from urllib.parse import urlparse

_ALLOWED_TAGS = frozenset({"p", "br", "strong", "em", "ul", "ol", "li", "a"})
_BLOCKED_CONTENT_TAGS = frozenset({"script", "style", "svg", "math", "iframe", "object"})
_SAFE_PROTOCOLS = frozenset({"http", "https", "mailto"})
_WHITESPACE_RE = re.compile(r"[ \t\f\v]+")


class _Sanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.blocked_depth = 0
        self.open_tags: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag in _BLOCKED_CONTENT_TAGS:
            self.blocked_depth += 1
            return
        if self.blocked_depth or tag not in _ALLOWED_TAGS:
            return
        if tag == "br":
            self.parts.append("<br>")
            return
        if tag == "a":
            href = next((value for name, value in attrs if name.casefold() == "href"), None)
            safe_href = _safe_href(href)
            if safe_href:
                self.parts.append(f'<a href="{html.escape(safe_href, quote=True)}">')
                self.open_tags.append(tag)
            return
        self.parts.append(f"<{tag}>")
        self.open_tags.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in _BLOCKED_CONTENT_TAGS:
            if self.blocked_depth:
                self.blocked_depth -= 1
            return
        if self.blocked_depth or tag not in _ALLOWED_TAGS or tag == "br":
            return
        if tag not in self.open_tags:
            return
        while self.open_tags:
            open_tag = self.open_tags.pop()
            self.parts.append(f"</{open_tag}>")
            if open_tag == tag:
                break

    def handle_data(self, data: str) -> None:
        if not self.blocked_depth:
            self.parts.append(html.escape(data, quote=False))

    def handle_entityref(self, name: str) -> None:
        if not self.blocked_depth:
            self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if not self.blocked_depth:
            self.parts.append(f"&#{name};")

    def finish(self) -> str:
        while self.open_tags:
            self.parts.append(f"</{self.open_tags.pop()}>")
        return "".join(self.parts).strip()


class _PlainTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.list_depth = 0
        self.in_li = False
        self.href_stack: list[str | None] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag in {"p", "ul", "ol"}:
            self._newline()
        elif tag == "br":
            self.parts.append("\n")
        elif tag == "li":
            self._newline()
            self.parts.append("  " * self.list_depth + "- ")
            self.in_li = True
        elif tag == "a":
            self.href_stack.append(
                next((value for name, value in attrs if name.casefold() == "href"), None)
            )
        if tag in {"ul", "ol"}:
            self.list_depth += 1

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in {"p", "li"}:
            self._newline()
            self.in_li = False
        elif tag in {"ul", "ol"}:
            self.list_depth = max(0, self.list_depth - 1)
            self._newline()
        elif tag == "a" and self.href_stack:
            self.href_stack.pop()

    def handle_data(self, data: str) -> None:
        if data:
            self.parts.append(data)

    def _newline(self) -> None:
        if self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")


def _safe_href(value: str | None) -> str | None:
    if not value:
        return None
    value = html.unescape(value).strip()
    parsed = urlparse(value)
    if parsed.scheme.casefold() not in _SAFE_PROTOCOLS:
        return None
    return value


def sanitize_email_html(value: str | None) -> str:
    """Return allowlisted email HTML with scripts, styles and unknown attributes removed."""
    parser = _Sanitizer()
    parser.feed(value or "")
    parser.close()
    return parser.finish()


def html_to_text(value: str | None) -> str:
    """Convert sanitized email HTML into a stable plain-text fallback."""
    parser = _PlainTextParser()
    parser.feed(sanitize_email_html(value))
    parser.close()
    text = "".join(parser.parts).replace("\r\n", "\n").replace("\r", "\n")
    lines = [_WHITESPACE_RE.sub(" ", line).rstrip() for line in text.split("\n")]
    compact: list[str] = []
    for line in lines:
        if not line and compact and not compact[-1]:
            continue
        compact.append(line)
    return "\n".join(compact).strip()


def plain_text_to_html(value: str | None) -> str:
    """Convert a legacy plain-text draft into escaped paragraph HTML."""
    normalized = (value or "").replace("\r\n", "\n").replace("\r", "\n")
    paragraphs = re.split(r"\n{2,}", normalized)
    rendered: list[str] = []
    for paragraph in paragraphs:
        if not paragraph and not rendered:
            continue
        escaped = html.escape(paragraph, quote=False).replace("\n", "<br>")
        rendered.append(f"<p>{escaped}</p>")
    return "".join(rendered) or "<p></p>"
