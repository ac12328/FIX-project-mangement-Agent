"""Public-demo retrieval hints. No private aliases or market procedures."""
from __future__ import annotations
import re
from dataclasses import dataclass, field


@dataclass
class ExpandedQuery:
    original: str
    text: str
    intents: list[str] = field(default_factory=list)
    boost_paths: list[str] = field(default_factory=list)


def is_contextual_demo_grey_market_query(user_text: str) -> bool:
    """Recognize the demo topic without implying market-specific instructions."""
    text = user_text or ""
    return bool(re.search(r"grey\s*market|gray\s*market", text, re.I)
                and re.search(r"uat|conformance|testing|guideline", text, re.I))


def expand_query(user_text: str) -> ExpandedQuery:
    original = (user_text or "").strip()
    intents: list[str] = []
    paths: list[str] = []
    if re.search(r"completed?|closing|residual|m8", original, re.I):
        intents.append("project_closing")
        paths.append("concept/project_closing")
    elif re.search(r"pct|production.*connectivity", original, re.I):
        intents.append("pct")
        paths.append("playbook/fix-connectivity/fix-onboarding-project-completion-audit")
    if re.search(r"milestone|m[1-8]\b|onboarding", original, re.I):
        intents.append("milestone")
        paths.append("concept/milestone_state_machine")
    if re.search(r"sendercompid|targetcompid|session|uat", original, re.I):
        intents.append("fix_uat")
        paths.append("playbook/conformance/public-demo-uat-guidelines")
    return ExpandedQuery(original=original, text=original, intents=intents,
                         boost_paths=list(dict.fromkeys(paths)))


def expand_knowledge_search_query(user_text: str) -> str:
    return expand_query(user_text).text
