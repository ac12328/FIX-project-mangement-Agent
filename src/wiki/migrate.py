"""Migrate knowledge/*.md files to wiki/markets/ with frontmatter."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from src.wiki.schema import WikiPage, render_page

logger = logging.getLogger(__name__)

_FILENAME_TO_TAGS = {
    "Global_Rules.md": ["global", "cross-market"],
    "HK_Market.md": ["HK", "hong-kong", "SEHK"],
    "US_Market.md": ["US", "united-states"],
    "JP_Market.md": ["JP", "japan", "TSE"],
    "SG_Market.md": ["SG", "singapore", "SGX"],
    "CN_Market.md": ["CN", "china", "china-connect"],
    "Manual_Markets.md": ["manual", "care-order"],
}


def _extract_title(content: str) -> str:
    m = re.match(r"^#\s+(.+)", content, re.MULTILINE)
    return m.group(1).strip() if m else ""


def add_frontmatter_to_market_file(filename: str, content: str) -> str:
    """Wrap a knowledge markdown file with wiki frontmatter."""
    title = _extract_title(content)
    tags = _FILENAME_TO_TAGS.get(filename, [])
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    page = WikiPage(
        title=title or filename.replace("_", " ").replace(".md", ""),
        type="market",
        tags=tags,
        sources=["migration:knowledge-dir"],
        created=today,
        updated=today,
        status="draft",
        confidence="low",
        body=content,
    )
    return render_page(page)


def migrate_knowledge_to_wiki(knowledge_dir: Path, wiki_dir: Path) -> None:
    """Migrate all .md files from knowledge_dir into wiki/markets/ with frontmatter.

    Idempotent: skips files that already exist in wiki/markets/.
    Creates the full wiki directory scaffold and rebuilds the index.
    """
    for subdir in ("entities", "markets", "concepts", "patterns", "sources", "raw"):
        (wiki_dir / subdir).mkdir(parents=True, exist_ok=True)

    schema_path = wiki_dir / "schema.md"
    if not schema_path.exists():
        schema_path.write_text(_SCHEMA_TEMPLATE, encoding="utf-8")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    migrated: list[str] = []
    for md_file in sorted(knowledge_dir.glob("*.md")):
        target_name = md_file.name.lower()
        target_path = wiki_dir / "markets" / target_name
        if target_path.exists():
            logger.info("Wiki file already exists, skipping: %s", target_path)
            continue
        content = md_file.read_text(encoding="utf-8")
        wiki_content = add_frontmatter_to_market_file(md_file.name, content)
        target_path.write_text(wiki_content, encoding="utf-8")
        migrated.append(target_name)

    _rebuild_index(wiki_dir)

    log_path = wiki_dir / "log.md"
    if not log_path.exists():
        log_path.write_text("# Wiki Log\n\n", encoding="utf-8")
    if migrated:
        entry = (
            f"## [{today}] migration | {len(migrated)} files from knowledge/\n"
            f"Migrated: {', '.join(migrated)}\n\n"
        )
        log_path.write_text(
            log_path.read_text(encoding="utf-8") + entry, encoding="utf-8",
        )


def _rebuild_index(wiki_dir: Path) -> None:
    """Rebuild wiki/index.md from all category subdirectories."""
    from src.wiki.schema import parse_page as _parse

    sections: dict[str, list[str]] = {
        "entities": [], "markets": [], "concepts": [],
        "patterns": [], "sources": [],
    }
    for category in sections:
        cat_dir = wiki_dir / category
        if not cat_dir.is_dir():
            continue
        for md_file in sorted(cat_dir.glob("*.md")):
            content = md_file.read_text(encoding="utf-8")
            page = _parse(content)
            title = page.title or md_file.stem.replace("_", " ").title()
            rel = f"{category}/{md_file.name}"
            sections[category].append(f"- [{title}]({rel})")

    lines = ["# Wiki Index\n"]
    for cat, entries in sections.items():
        if entries:
            lines.append(f"## {cat.title()}\n")
            lines.extend(entries)
            lines.append("")

    (wiki_dir / "index.md").write_text("\n".join(lines), encoding="utf-8")


_SCHEMA_TEMPLATE = """\
# Wiki Schema

This document defines conventions for the LLM wiki curator.

## Page Types
- **entity** — counterparty, exchange, or institution
- **market** — market-specific rules and routing
- **concept** — FIX protocol concepts, procedures
- **pattern** — cross-project execution patterns, playbooks
- **source** — summary of an ingested raw document

## Page Format
Every page MUST have YAML frontmatter with: title, type, tags, sources, created, updated, confidence.
The body uses standard Markdown. Use `[[page_name]]` for cross-references.

## Confidence Levels
- **high** — verified by operator or multiple sources
- **medium** — LLM-synthesized, single source
- **low** — inferred, needs verification

## Naming Convention
Filenames: lowercase, underscores, `.md` extension (e.g. `demo_gateway_a.md`).

## Update Rules
- When updating a page, always update the `updated` field.
- When adding information from a new source, append to `sources` list.
- Contradictions: flag in a `## Contradictions` section, do not silently overwrite.
- Cross-references: use `[[page_name]]` links. Add to `## See Also` section.
"""
