"""
Layer 5: Knowledge context loader.
Loads global and market-specific knowledge files for a project.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from config.settings import BASE_DIR

logger = logging.getLogger(__name__)


MARKET_KNOWLEDGE_MAP = {
    "HK": ["HK_Market.md"],
    "US": ["US_Market.md"],
    "JP": ["JP_Market.md"],
    "SG": ["SG_Market.md"],
    "CN": ["CN_Market.md"],
    "PM": ["PM_Market.md"],
    "KR": ["Manual_Markets.md"],
    "TW": ["Manual_Markets.md"],
    "GR": ["Manual_Markets.md"],
    "UK": ["Manual_Markets.md"],
    "AU": ["Manual_Markets.md"],
    "TH": ["Manual_Markets.md"],
    "MY": ["Manual_Markets.md"],
    "FN": ["Manual_Markets.md"],
}


@lru_cache(maxsize=16)
def _load_knowledge_file(file_name: str) -> str:
    """Load a knowledge file from disk (cached)."""
    path = BASE_DIR / "wiki" / "markets" / file_name.lower()
    return path.read_text(encoding="utf-8")


def load_knowledge_for_project(market: str | None) -> str:
    """Load global and market-specific knowledge files for one project."""
    file_names: list[str] = ["Global_Rules.md"]
    seen = set(file_names)

    tokens = []
    if market:
        tokens = [token.strip().upper() for token in market.split(",") if token.strip()]

    for token in tokens:
        for file_name in MARKET_KNOWLEDGE_MAP.get(token, []):
            if file_name not in seen:
                file_names.append(file_name)
                seen.add(file_name)

    loaded_parts: list[str] = []
    for file_name in file_names:
        try:
            loaded_parts.append(_load_knowledge_file(file_name))
        except Exception as exc:
            logger.warning("Knowledge file load failed for %s: %s", file_name, exc)

    return "\n\n---\n\n".join(loaded_parts) if loaded_parts else ""
