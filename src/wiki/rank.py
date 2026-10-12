"""Query-intent scoring helpers for wiki retrieval re-rank."""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class QueryIntent:
    """Lightweight intent flags derived from the raw query string."""

    procedure: bool = False   # how / steps / submit / procedure
    failure: bool = False     # error / reject / timeout / fail
    definition: bool = False  # what is / means / definition
    entity: bool = False      # who / counterparty / CompID

    @property
    def preferred_types(self) -> tuple[str, ...]:
        if self.procedure:
            return ("playbook", "pattern", "concept")
        if self.failure:
            return ("pattern", "playbook", "concept")
        if self.definition:
            return ("concept", "entity", "market")
        if self.entity:
            return ("entity", "market", "concept")
        return ()


_PROCEDURE_RE = re.compile(
    r"\b(how|steps?|procedure|submit|workflow|process|checklist|prerequisite|"
    r"點做|怎麼|步驟|流程|提交)\b",
    re.IGNORECASE,
)
_FAILURE_RE = re.compile(
    r"\b(error|reject|rejected|timeout|fail|failed|failure|disconnect|"
    r"logon\s*reject|唔通|失敗|拒絕)\b",
    re.IGNORECASE,
)
_DEFINITION_RE = re.compile(
    r"\b(what\s+is|what'?s|means?|define|definition|係咩|是什么|什麼是)\b",
    re.IGNORECASE,
)
_ENTITY_RE = re.compile(
    r"\b(who|counterparty|compid|sendercompid|contact|entity|"
    r"邊個|谁|對手)\b",
    re.IGNORECASE,
)


def detect_query_intent(query: str) -> QueryIntent:
    q = query or ""
    return QueryIntent(
        procedure=bool(_PROCEDURE_RE.search(q)),
        failure=bool(_FAILURE_RE.search(q)),
        definition=bool(_DEFINITION_RE.search(q)),
        entity=bool(_ENTITY_RE.search(q)),
    )


def type_preference_boost(page_type: str, intent: QueryIntent, *, amount: float = 0.12) -> float:
    """Score delta for page_type given intent (first preferred type gets full boost)."""
    preferred = intent.preferred_types
    if not preferred or not page_type:
        return 0.0
    pt = page_type.lower()
    try:
        rank = preferred.index(pt)
    except ValueError:
        return 0.0
    # 1st: full, 2nd: half, 3rd: quarter
    return amount / (2 ** rank)


def section_preference_boost(section: str, intent: QueryIntent) -> float:
    sec = (section or "").lower()
    if intent.procedure and (
        sec.startswith("step") or "precondition" in sec or "when to use" in sec
    ):
        return 0.08
    if intent.failure and ("fail" in sec or "error" in sec or "symptom" in sec):
        return 0.06
    return 0.0


_STOP = frozenset({
    "the", "and", "for", "with", "from", "that", "this", "what", "how", "when",
    "are", "is", "was", "into", "about", "must", "should", "have", "has",
    "page", "wiki", "please", "need", "using",
})


def _query_tokens(text: str) -> set[str]:
    return {
        t for t in re.findall(r"[a-z0-9]{3,}", (text or "").lower())
        if t not in _STOP
    }


def hub_boost_amount(
    path_no_ext: str,
    query: str,
    boost_set: set[str],
    boost_stems: set[str],
    *,
    full_boost: float = 0.20,
) -> float:
    """Hub path boost, damped when the query is highly specific.

    Broad intent ("HK IPO") → full hub boost.
    Specific intent ("underutilization penalty CHATS") → small hub boost so
    leaf pages can still win #1.
    """
    stem = path_no_ext.rsplit("/", 1)[-1]
    if path_no_ext not in boost_set and stem not in boost_stems:
        return 0.0
    tokens = _query_tokens(query)
    # A routed leaf whose descriptive slug is largely named by the query is
    # not a generic hub. Expansion length alone must not erase that match.
    path_tokens = _query_tokens(stem.replace("-", " ").replace("_", " "))
    matched = tokens & path_tokens
    named_leaf = (
        len(path_tokens) == 2 and matched == path_tokens
        and all(len(token) >= 5 for token in path_tokens)
    )
    if named_leaf or (len(matched) >= 3 and len(matched) / max(1, len(path_tokens)) >= 0.75):
        return full_boost
    # Distinctive long tokens signal specificity
    long_toks = [t for t in tokens if len(t) >= 5]
    if len(long_toks) >= 5:
        return full_boost * 0.30
    if len(long_toks) >= 3:
        return full_boost * 0.55
    if len(tokens) >= 6:
        return full_boost * 0.70
    return full_boost


def specificity_boost(
    query: str,
    title: str,
    content: str,
    path_no_ext: str,
    boost_set: set[str],
    boost_stems: set[str],
    *,
    amount: float = 0.18,
) -> float:
    """Boost leaf pages whose title/content cover distinctive query tokens."""
    stem = path_no_ext.rsplit("/", 1)[-1]
    is_hub = path_no_ext in boost_set or stem in boost_stems
    q = _query_tokens(query)
    if len(q) < 2:
        return 0.0
    doc = _query_tokens(f"{title} {content[:400]} {stem.replace('-', ' ').replace('_', ' ')}")
    if not doc:
        return 0.0
    coverage = len(q & doc) / len(q)
    if is_hub:
        # Hub only needs mild help when it actually matches terms
        if coverage >= 0.35:
            return amount * 0.25
        return 0.0
    if coverage >= 0.55:
        return amount
    if coverage >= 0.35:
        return amount * 0.55
    if coverage >= 0.25:
        return amount * 0.30
    return 0.0


def market_path_boost(query: str, path_no_ext: str) -> float:
    """Prefer markets/* when query looks like market knowledge lookup."""
    if not path_no_ext.startswith("markets/"):
        return 0.0
    q = query.lower()
    if re.search(r"\b(market\s+knowledge|market\s+rules|routing\s+rules)\b", q):
        return 0.14
    # e.g. "HK market" / "US market"
    code = path_no_ext.split("/", 1)[-1].replace("_market", "").replace("market", "")
    if code and re.search(rf"\b{re.escape(code)}\b", q):
        return 0.12
    return 0.0


def named_title_boost(query: str, title: str, *, amount: float = 0.45) -> float:
    """Recognize an explicitly named multiword page, allowing acronym glosses.

    Use the original query, never alias-expanded text. Scope/trust eligibility
    remains the caller's responsibility; a draft retains its unverified warning.
    """
    wanted = re.findall(r"[a-z0-9]+", (title or "").lower())
    if len(wanted) < 3:
        return 0.0
    tokens = re.findall(r"[A-Za-z0-9]+", query or "")
    for start, token in enumerate(tokens):
        if token.lower() != wanted[0]:
            continue
        matched = 1
        for candidate in tokens[start + 1:]:
            if candidate.lower() == wanted[matched]:
                matched += 1
                if matched == len(wanted):
                    return amount
            elif (
                (candidate.isupper() and 2 <= len(candidate) <= 5)
                or candidate.lower() in _STOP
            ):
                continue
            else:
                break
    return 0.0
