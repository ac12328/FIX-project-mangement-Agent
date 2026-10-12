"""Internal rules for the offline Policy-v4 grounding-repair certificate.

The serving runtime must not import this module. It contains only deterministic,
side-effect-free research-screening helpers.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

POLICY_VERSION = "workspace-grounding-repair-certificate-v4"
MIN_SUBSTANTIVE_RETENTION = 0.80


CITATION_RE = re.compile(r"\[\[source:([A-Za-z0-9][A-Za-z0-9_.:-]*)\]\]")
INTERNAL_MARKER_RE = re.compile(r"\bmonads0x[a-z0-9_]+\b", re.IGNORECASE)
OMISSION_NOTICE = (
    "> Some statements were omitted because the Workspace could not verify "
    "their source citations."
)
ALLOWED_INITIAL_ERROR_CODES = frozenset(
    {"uncited_factual_line", "unknown_citation", "internal_tool_marker"}
)
ALLOWED_AGGREGATE_SOURCE_KINDS = frozenset(
    {
        "portfolio",
        "portfolio_query",
        "project_work_query",
        "timeline_query",
        "unanswered_thread_query",
    }
)
DEFAULT_METADATA_SCALARS = frozenset(
    {"total", "result_count", "projects_scanned", "attention_count"}
)
_PROJECTION_FIELDS = ("title", "snippet", "project_id", "occurred_at")
NUMBER_RE = re.compile(
    r"(?<![\w.])\d{4}-\d{2}-\d{2}(?![\w.])|"
    r"(?<![\w.])\d+(?:\.\d+)?%(?![\w.])|"
    r"(?<![\w.])\d+(?:\.\d+)?(?![\w.])"
)
_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
_TABLE_SEPARATOR_RE = re.compile(r"^\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?$")
_ADVISORY_RE = re.compile(
    r"^(?:recommendation|recommended\s+next\s+step|next\s+steps?|suggestion|"
    r"i\s+(?:recommend|suggest)|we\s+(?:recommend|suggest)|"
    r"(?:please\s+)?consider|you\s+(?:should|could|may)|"
    r"建議(?:下一步)?|下一步(?:建議)?|可考慮|應該|請考慮)\b|"
    r"^(?:建議|下一步|可考慮|應該|請考慮)",
    re.IGNORECASE,
)
CURRENT_RE = re.compile(
    r"\b(?:current|currently|latest|as[- ]of|today|now)\b|目前|現時|最新|截至|今日|現在",
    re.IGNORECASE,
)
COMPLETION_RE = re.compile(
    r"\b(?:completed?|completion|ready|readiness|go[- ]?live|production[- ]ready)\b|"
    r"\ball\s+(?:required\s+)?(?:conformance\s+|fix\s+|production\s+)?"
    r"tests?\s+(?:(?:have|has|had|are|were)\s+)?(?:been\s+)?passed\b|"
    r"完成|已完成|準備就緒|可上線|正式上線|所有測試(?:已)?通過",
    re.IGNORECASE,
)
RANKING_RE = re.compile(
    r"\b(?:rank(?:ed|ing)?|top|bottom|highest|lowest|most|least|"
    r"higher|lower|more\s+than|less\s+than|fewer\s+than|best|worst)\b|"
    r"排名|最高|最低|最多|最少|較高|較低|優先次序",
    re.IGNORECASE,
)
AGGREGATION_RE = re.compile(
    r"\b(?:total|overall|combined|across|portfolio|aggregate|aggregation|sum|"
    r"count|number\s+of|all\s+projects?|every\s+project|attention)\b|"
    r"總共|總數|整體|合計|所有項目|全部項目|跨項目|項目組合|需要關注",
    re.IGNORECASE,
)
_PROJECT_FACT_RE = re.compile(
    r"\b(?:M[1-8](?:_[A-Za-z0-9]+)?|Active|Blocked|Waiting|Completed|Archived|"
    r"Cancelled|Internal|External|Shared)\b|"
    r"\b(?:P|PROJ(?:ECT)?)[-_]?\d{2,}\b|"
    r"\b[A-Z]{2,}[A-Z0-9-]*20\d{2}\b|"
    r"項目狀態|里程碑|被阻塞|等待中|已取消",
    re.IGNORECASE,
)
_STOPWORDS = frozenset(
    {
        "a",
        "about",
        "an",
        "and",
        "are",
        "as",
        "at",
        "based",
        "be",
        "because",
        "by",
        "for",
        "from",
        "has",
        "have",
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "overall",
        "record",
        "records",
        "source",
        "sources",
        "that",
        "the",
        "their",
        "there",
        "this",
        "to",
        "was",
        "were",
        "with",
        "workspace",
    }
)


@dataclass(frozen=True, slots=True)
class Projection:
    text: str
    tokens: frozenset[str]
    numbers: frozenset[str]
    kind: str


def is_structural(line: str) -> bool:
    stripped = line.strip()
    return (
        not stripped
        or stripped.startswith(("#", "```"))
        or bool(_TABLE_SEPARATOR_RE.fullmatch(stripped))
        or (stripped.endswith(":") and len(stripped) <= 80)
    )


def claim_tokens(value: str) -> set[str]:
    normalized = value.replace("_", " ").replace("-", " ").replace(":", " ")
    tokens: set[str] = set()
    for match in _TOKEN_RE.findall(normalized.casefold()):
        if len(match) < 2 or match in _STOPWORDS or match.isdecimal():
            continue
        tokens.add(match[:-1] if match.isascii() and len(match) > 4 and match.endswith("s") else match)
    return tokens


def projection_text(source: dict[str, Any], allowlist: frozenset[str]) -> str:
    values: list[str] = []
    for field in _PROJECTION_FIELDS:
        value = source.get(field)
        if value not in (None, ""):
            values.extend((field, str(value)))

    metadata_values: list[dict[str, Any]] = []
    metadata = source.get("metadata")
    if isinstance(metadata, dict):
        metadata_values.append(metadata)
    metadata_json = source.get("metadata_json")
    if isinstance(metadata_json, dict):
        metadata_values.append(metadata_json)
    elif isinstance(metadata_json, str) and metadata_json.strip().startswith("{"):
        try:
            parsed = json.loads(metadata_json)
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = None
        if isinstance(parsed, dict):
            metadata_values.append(parsed)

    for field in allowlist:
        value = source.get(field)
        if isinstance(value, (str, int, float, bool)) and value != "":
            values.extend((field, str(value)))
        for metadata_item in metadata_values:
            value = metadata_item.get(field)
            if isinstance(value, (str, int, float, bool)) and value != "":
                values.extend((field, str(value)))
    return " ".join(values)


def _strip_advisory_prefix(line: str) -> str:
    stripped = line.strip()
    stripped = re.sub(r"^(?:[-*+>]\s+|\d+[.)]\s+)", "", stripped)
    return stripped.strip("*_")


def is_advisory(line: str) -> bool:
    return bool(_ADVISORY_RE.search(_strip_advisory_prefix(line)))


def advisory_has_factual_signal(line: str) -> bool:
    return bool(
        CITATION_RE.search(line)
        or NUMBER_RE.search(line)
        or _PROJECT_FACT_RE.search(line)
        or CURRENT_RE.search(line)
        or COMPLETION_RE.search(line)
        or AGGREGATION_RE.search(line)
        or RANKING_RE.search(line)
    )


def unsupported_tokens(claim: str, projection: Projection) -> set[str]:
    unsupported: set[str] = set()
    for token in claim_tokens(claim):
        if token in projection.tokens:
            continue
        if not token.isascii() and token in projection.text.casefold():
            continue
        unsupported.add(token)
    return unsupported




__all__ = [
    "MIN_SUBSTANTIVE_RETENTION",
    "POLICY_VERSION",
    "Projection",
    "advisory_has_factual_signal",
    "claim_tokens",
    "is_advisory",
    "is_structural",
    "projection_text",
    "unsupported_tokens",
    "ALLOWED_AGGREGATE_SOURCE_KINDS",
    "ALLOWED_INITIAL_ERROR_CODES",
    "CITATION_RE",
    "CURRENT_RE",
    "COMPLETION_RE",
    "DEFAULT_METADATA_SCALARS",
    "INTERNAL_MARKER_RE",
    "NUMBER_RE",
    "OMISSION_NOTICE",
    "RANKING_RE",
    "AGGREGATION_RE",
]
