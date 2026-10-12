"""WikiDomainIndex — maps email domains to org_type from wiki entity pages.

Singleton with same pattern as WikiGraph. Call invalidate_wiki_domain_index()
after any wiki write to force a rebuild on next access.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

from config.settings import BASE_DIR
from src.wiki.schema import parse_page

logger = logging.getLogger(__name__)

_WIKI_DIR = BASE_DIR / "wiki"
_ENTITY_DIR_NAME = "entity"


def _get_company_domains_fallback() -> set[str]:
    """Return CONFIG.company_domains as bootstrap fallback."""
    try:
        from config.settings import CONFIG
        return set(getattr(CONFIG, "company_domains", set()))
    except Exception:
        return set()


class WikiDomainIndex:
    """Maps email domains to org_type, built from wiki entity frontmatter."""

    def __init__(self, wiki_dir: Path | None = None) -> None:
        self._wiki_dir = Path(wiki_dir) if wiki_dir else _WIKI_DIR
        self._index: dict[str, str] = {}   # domain -> org_type
        self._build()

    def _build(self) -> None:
        index: dict[str, str] = {}
        entity_dir = self._wiki_dir / _ENTITY_DIR_NAME
        if entity_dir.is_dir():
            for md_path in sorted(entity_dir.rglob("*.md")):
                try:
                    page = parse_page(md_path.read_text(encoding="utf-8"))
                    if (page.status or "active").lower() == "archived":
                        continue
                    if page.org_type and page.domains:
                        for raw_domain in page.domains:
                            normalised = raw_domain.strip().lower()
                            if normalised:
                                index[normalised] = page.org_type
                except Exception:
                    logger.debug("Failed to parse wiki page %s", md_path, exc_info=True)

        # Bootstrap fallback: if wiki has no domain entries, use CONFIG.company_domains
        if not index:
            for domain in _get_company_domains_fallback():
                index[domain.lower()] = "internal"
            if index:
                logger.info(
                    "WikiDomainIndex: no wiki domains found, falling back to CONFIG.company_domains (%d domains)",
                    len(index),
                )

        self._index = index
        logger.debug("WikiDomainIndex built: %d domains indexed", len(index))

    def is_internal(self, domain: str) -> bool:
        """Return True if the domain belongs to an internal organisation."""
        return self.org_type_for(domain) == "internal"

    def is_vendor(self, domain: str) -> bool:
        """Return True if the domain belongs to a vendor organisation."""
        return self.org_type_for(domain) == "vendor"

    def org_type_for(self, domain: str) -> str:
        """Return org_type for domain, or 'unknown' if not indexed.

        Priority: wiki frontmatter → company_domains (internal) →
        VENDOR_EMAIL_DOMAINS → unknown.
        """
        normalised = (domain or "").lower().strip()
        if not normalised:
            return "unknown"
        if normalised in self._index:
            return self._index[normalised]
        company = {d.lower() for d in _get_company_domains_fallback()}
        if normalised in company:
            return "internal"
        try:
            from config.defaults import VENDOR_EMAIL_DOMAINS

            if normalised in VENDOR_EMAIL_DOMAINS:
                return "vendor"
        except Exception:
            pass
        return "unknown"


# ── Singleton ─────────────────────────────────────────────────────────────

_wiki_domain_index: WikiDomainIndex | None = None
_domain_lock = threading.Lock()


def get_wiki_domain_index() -> WikiDomainIndex:
    """Return the cached WikiDomainIndex, building it on first access."""
    global _wiki_domain_index
    if _wiki_domain_index is not None:
        return _wiki_domain_index
    with _domain_lock:
        if _wiki_domain_index is None:
            _wiki_domain_index = WikiDomainIndex()
    return _wiki_domain_index


def invalidate_wiki_domain_index() -> None:
    """Clear the cached index. Next call to get_wiki_domain_index() will rebuild."""
    global _wiki_domain_index
    with _domain_lock:
        _wiki_domain_index = None
