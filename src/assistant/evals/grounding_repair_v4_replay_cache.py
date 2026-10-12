"""In-memory caches and indexes for Policy-v4 offline replay."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from src.assistant import grounding


_EmailKey = tuple[str, str, str, str, str, str]
_EmailSignals = tuple[bool, bool, bool, bool, bool]


@dataclass
class ReplayCache:
    """In-memory-only memoization shared across one offline replay run."""

    email_signals: dict[_EmailKey, _EmailSignals] = field(default_factory=dict)
    citation_plans: dict[
        tuple[tuple[str, str, str], ...],
        tuple[tuple[str, str, str, re.Pattern[str] | None], ...],
    ] = field(default_factory=dict)


_ASCII_PROJECT_BOUNDARY = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
)
_EXEMPTION_EMAIL_RE = re.compile(r"test cases?\s*8\s*(?:-|\u2013|\u2014|to)\s*11")
_SUBMISSION_EMAIL_RE = re.compile(
    r"(?:attached|submit(?:ted)?|sent|provided).{0,80}(?:test|conformance).{0,40}results|"
    r"please check.{0,60}(?:test|conformance).{0,40}results",
    re.IGNORECASE | re.DOTALL,
)
_CONTACT_SIGNAL_RE = re.compile(
    r"contact (?:details|information|list)|(?:information enquiry|information inquiry)|"
    r"provided.{0,40}contacts?"
)
_MULTISPACE_RE = re.compile(r"[ \t]{2,}")
_UNANSWERED_SCOPE_RE = re.compile(
    r"non[- ]terminal|open projects?|active projects?|include(?:s|d|ing)? terminal|terminal projects?|"
    r"\u975e\s*terminal|\u958b\u555f\u9805\u76ee|\u9032\u884c\u4e2d\u9805\u76ee|"
    r"\u6d3b\u8e8d\u9805\u76ee",
    re.IGNORECASE,
)


def _citation_plan(
    sources: list[dict[str, Any]],
    cache: ReplayCache,
) -> tuple[tuple[str, str, str, re.Pattern[str] | None], ...]:
    key_items: list[tuple[str, str, str]] = []
    for source in sources:
        project_id = str(source.get("project_id") or "").strip()
        source_id = str(source.get("source_id") or "").strip()
        if project_id and source_id:
            key_items.append((project_id, source_id, str(source.get("kind") or "")))
    key = tuple(key_items)
    found = cache.citation_plans.get(key)
    if found is not None:
        return found

    source_priority = {"party": 0, "project": 1, "milestone": 2, "activity": 3, "email": 4}
    project_sources: dict[str, list[tuple[str, str]]] = {}
    for project_id, source_id, kind in key:
        project_sources.setdefault(project_id, []).append((source_id, kind))
    plan: list[tuple[str, str, str, re.Pattern[str] | None]] = []
    for project_id, items in project_sources.items():
        preferred = min(items, key=lambda item: source_priority.get(item[1], 9))[0]
        fallback = None
        if not project_id.isascii():
            fallback = re.compile(
                rf"(?<![A-Za-z0-9_-]){re.escape(project_id)}(?![A-Za-z0-9_-])",
                re.IGNORECASE,
            )
        plan.append((project_id, project_id.casefold(), preferred, fallback))
    resolved = tuple(plan)
    cache.citation_plans[key] = resolved
    return resolved


def _ascii_bounded_contains(line: str, needle: str) -> bool:
    start = 0
    while True:
        found = line.find(needle, start)
        if found < 0:
            return False
        end = found + len(needle)
        before_ok = found == 0 or line[found - 1] not in _ASCII_PROJECT_BOUNDARY
        after_ok = end == len(line) or line[end] not in _ASCII_PROJECT_BOUNDARY
        if before_ok and after_ok:
            return True
        start = found + 1


def _attach_project_line_citations(
    answer: str,
    plan: tuple[tuple[str, str, str, re.Pattern[str] | None], ...],
) -> str:
    if not plan:
        return answer
    lines: list[str] = []
    for raw_line in answer.splitlines():
        if "[[source:" in raw_line and grounding._CITATION_RE.search(raw_line):
            lines.append(raw_line)
            continue
        folded = raw_line.casefold()
        preferred = ""
        match_count = 0
        for _project_id, project_folded, source_id, fallback in plan:
            matched = (
                bool(fallback.search(raw_line))
                if fallback is not None
                else _ascii_bounded_contains(folded, project_folded)
            )
            if not matched:
                continue
            match_count += 1
            preferred = source_id
            if match_count > 1:
                break
        if match_count == 1 and raw_line.strip():
            raw_line = f"{raw_line.rstrip()} [[source:{preferred}]]"
        lines.append(raw_line)
    return "\n".join(lines)


class _AnswerIndex:
    """Incremental lookup state for the exact answer text under construction."""

    def __init__(self, text: str) -> None:
        self._rebuild(text)

    def _rebuild(self, text: str) -> None:
        self.text = text
        self.lower = text.lower()
        self.lines = text.splitlines()
        self.folded_lines = [line.casefold() for line in self.lines]
        self.cited: set[str] = set()
        self.line_citations: list[frozenset[str]] = []
        self.source_lines: dict[str, list[str]] = {}
        for line in self.lines:
            citations = (
                frozenset(grounding._CITATION_RE.findall(line))
                if "[[source:" in line
                else frozenset()
            )
            self.line_citations.append(citations)
            self.cited.update(citations)
            for source_id in citations:
                self.source_lines.setdefault(source_id, []).append(line)
        self._refresh_citation_text()

    def _refresh_citation_text(self) -> None:
        self.citation_text = " ".join(
            f"[[source:{source_id}]]" for source_id in sorted(self.cited)
        )

    def append_section(self, section: str) -> None:
        updated = f"{self.text}\n\n{section}".strip()
        expected = f"{self.text}\n\n{section}"
        if self.text and section and updated == expected:
            added_lines = ["", *section.splitlines()]
            self.text = updated
            self.lower += "\n\n" + section.lower()
            self.lines.extend(added_lines)
            self.folded_lines.extend(line.casefold() for line in added_lines)
            has_new_citation = False
            for line in added_lines:
                citations = (
                    frozenset(grounding._CITATION_RE.findall(line))
                    if "[[source:" in line
                    else frozenset()
                )
                self.line_citations.append(citations)
                if not citations.issubset(self.cited):
                    has_new_citation = True
                self.cited.update(citations)
                for source_id in citations:
                    self.source_lines.setdefault(source_id, []).append(line)
            if has_new_citation:
                self._refresh_citation_text()
            return
        self._rebuild(updated)



def _email_key(item: dict[str, Any]) -> _EmailKey:
    return (
        str(item.get("source_id") or ""),
        str(item.get("subject") or ""),
        str(item.get("sender_email") or ""),
        str(item.get("occurred_at") or ""),
        str(item.get("body") or ""),
        str(item.get("body_snippet") or ""),
    )


def _email_signals(item: dict[str, Any], cache: ReplayCache) -> _EmailSignals:
    key = _email_key(item)
    found = cache.email_signals.get(key)
    if found is not None:
        return found

    body = str(item.get("body") or "")
    head = grounding._email_head(item)
    test = bool(grounding._TEST_PASS_RE.search(head))
    completion = bool(grounding._COMPLETION_RE.search(head))
    contact_haystack = f"{str(item.get('subject') or '')}\n{head}".lower()
    structured_fields = all(
        token in contact_haystack for token in ("name", "role", "tel", "email")
    )
    contact = (
        bool(_CONTACT_SIGNAL_RE.search(contact_haystack)) or structured_fields
    ) and not completion and not test

    material_body = str(item.get("body") or item.get("body_snippet") or "")
    normalized = material_body.casefold()
    submission = all(token in normalized for token in ("loo", "loc", "moo", "moc")) and bool(
        _SUBMISSION_EMAIL_RE.search(normalized)
    )
    exemption = (
        bool(_EXEMPTION_EMAIL_RE.search(normalized))
        and "amend" in normalized
        and "cancel" in normalized
        and "exemption" in normalized
        and "administrative messages" in normalized
    )
    signals = (test, completion, contact, submission, exemption)
    cache.email_signals[key] = signals
    return signals


def _material_email_lines(
    index: _AnswerIndex,
    emails: list[dict[str, Any]],
    cache: ReplayCache,
) -> list[str]:
    lines: list[str] = []
    working = index.text
    working_lower = index.lower
    cited = set(index.cited)
    latest = grounding._latest_record(emails, "occurred_at")
    test_email = completion_email = contact_email = submission_email = exemption_email = None
    for item in emails:
        if not isinstance(item, dict):
            continue
        test, completion, contact, submission, exemption = _email_signals(item, cache)
        if test_email is None and test:
            test_email = item
        if completion_email is None and completion:
            completion_email = item
        if contact_email is None and contact:
            contact_email = item
        if submission_email is None and submission:
            submission_email = item
        if exemption_email is None and exemption:
            exemption_email = item

    for kind, item in (
        ("exemption", exemption_email),
        ("submission", submission_email),
        ("completion", completion_email),
        ("test", test_email),
        ("contact", contact_email),
        ("latest", latest),
    ):
        if not item:
            continue
        source_id = str(item.get("source_id") or "").strip()
        if not source_id:
            continue
        if kind == "exemption":
            complete = grounding._conformance_exemption_is_described(working) and source_id in cited
            text = grounding._conformance_exemption_fact()
        elif kind == "submission":
            complete = grounding._submission_email_is_described(working, item) and source_id in cited
            text = grounding._submission_email_fact(item)
        elif kind == "completion":
            complete = bool(grounding._COMPLETION_RE.search(working_lower)) and source_id in cited
            text = "the email explicitly recorded that PCT was completed and the session could go live"
        elif kind == "test":
            complete = bool(grounding._TEST_PASS_RE.search(working_lower)) and source_id in cited
            text = "the email explicitly recorded that all tests had passed"
        elif kind == "contact":
            complete = bool(grounding._CONTACT_RE.search(working_lower)) and source_id in cited
            sender = str(item.get("sender_email") or "").lower()
            actor = "Eloise" if sender.startswith("eloise") else "the sender"
            text = (
                f"{actor} provided contact/setup details for the FIX connection; this contact-only update "
                "does not by itself prove milestone completion"
            )
        else:
            complete = source_id in cited
            subject = grounding._single_line(str(item.get("subject") or "email update"), limit=180)
            text = f'the latest recorded email was "{subject}"'
        if complete:
            continue
        line = (
            f"- **Email update ({grounding._record_date(item, 'occurred_at')}):** "
            f"{text}. [[source:{source_id}]]"
        )
        lines.append(line)
        working = f"{working}\n{line}"
        working_lower = f"{working_lower}\n{line.lower()}"
        cited.add(source_id)
    return lines


__all__ = ["ReplayCache"]
