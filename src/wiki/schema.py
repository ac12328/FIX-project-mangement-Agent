"""Wiki page schema: parse, validate, render markdown pages with YAML frontmatter."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

import yaml

from src.wiki.models import (
    ConfidenceMethod,
    WikiConfidence,
    WikiGovernance,
    WikiScope,
    WikiSource,
    WikiVisibility,
    VerificationStatus,
)


_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n?", re.DOTALL)
_LINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
_BOLD_SUMMARY_RE = re.compile(r"^\s*\*\*(.+?)\*\*\s*$", re.MULTILINE)
_WEAK_SOURCES = frozenset({"", "agent_write", "email_thread", "llm", "unknown"})
_SOURCE_LOCATOR_RE = re.compile(
    r"^(email|thread|doc|operator|rule|evidence):.+",
    re.IGNORECASE,
)

VALID_TYPES = {"entity", "market", "concept", "pattern", "source", "playbook", "insight"}
VALID_CONFIDENCE = {"high", "medium", "low"}
VALID_STATUS = {"draft", "active", "trusted", "archived"}
VALID_LINK_RELS = {
    "depends_on", "depended_by", "relates_to", "operates_in",
    "uses", "used_by", "requires", "supersedes", "contradicts", "derived_from",
}
VALID_ORG_TYPES = {"internal", "client", "vendor", "regulator", "unknown"}
VALID_VISIBILITY = {"public", "internal_only"}

# Canonical type → top-level directory (singular where applicable)
TYPE_TO_DIR: dict[str, str] = {
    "entity": "entity",
    "market": "markets",
    "concept": "concept",
    "pattern": "pattern",
    "playbook": "playbook",
    "source": "sources",
    "insight": "insight",
}
DIR_TO_TYPE: dict[str, str] = {v: k for k, v in TYPE_TO_DIR.items()}
# Legacy plural dirs — never write here; lint warns if content appears
LEGACY_PLURAL_DIRS = frozenset({"entities", "concepts", "patterns"})
CANONICAL_CONTENT_DIRS = frozenset(TYPE_TO_DIR.values()) | {"raw"}


@dataclass
class WikiLink:
    target: str       # relative path without extension, e.g. "entity/demo_vendor"
    rel: str          # must be in VALID_LINK_RELS
    note: str = ""


def wiki_link_key(link: WikiLink) -> tuple[str, str]:
    """Return the normalized identity of a typed Wiki edge."""
    target = link.target.replace("\\", "/").strip("/").removesuffix(".md").strip("/")
    return target, link.rel.strip()


def duplicate_wiki_link_keys(links: list[WikiLink]) -> list[tuple[str, str]]:
    """Return repeated edge keys once, in first-duplicate order."""
    seen: set[tuple[str, str]] = set()
    duplicates: list[tuple[str, str]] = []
    for link in links:
        key = wiki_link_key(link)
        if key in seen and key not in duplicates:
            duplicates.append(key)
        seen.add(key)
    return duplicates


def dedupe_wiki_links(links: list[WikiLink]) -> list[WikiLink]:
    """Merge duplicate edges while preserving order and distinct notes."""
    result: list[WikiLink] = []
    positions: dict[tuple[str, str], int] = {}
    notes: dict[tuple[str, str], list[str]] = {}
    for link in links:
        target, rel = wiki_link_key(link)
        key = target, rel
        note = link.note.strip()
        if key not in positions:
            positions[key] = len(result)
            notes[key] = [note] if note else []
            result.append(WikiLink(target=target, rel=rel, note=note))
            continue
        if note and note not in notes[key]:
            notes[key].append(note)
            result[positions[key]].note = "; ".join(notes[key])
    return result


@dataclass
class WikiPage:
    title: str = ""
    type: str = ""
    tags: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    created: str = ""
    updated: str = ""
    confidence: str = ""
    status: str = "active"
    org_type: str | None = None
    domains: list[str] = field(default_factory=list)
    visibility: str = "public"
    visibility_explicit: bool = False
    governance: WikiGovernance | None = None
    links: list[WikiLink] = field(default_factory=list)
    body: str = ""


def _parse_yaml_simple(text: str) -> dict:
    """Minimal YAML-subset parser for frontmatter (avoids PyYAML dependency).
    Handles: scalars, quoted strings, inline lists [a, b, c].
    Does NOT parse nested structures (links: block handled separately).
    """
    result: dict = {}
    for line in text.split("\n"):
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if not key or key == "links":
            continue
        if value and value[0] in ('"', "'") and value[-1] == value[0]:
            value = value[1:-1]
        if value.startswith("[") and value.endswith("]"):
            items = [v.strip().strip('"').strip("'") for v in value[1:-1].split(",") if v.strip()]
            result[key] = items
        else:
            result[key] = value
    return result


def _parse_links_block(yaml_text: str) -> list[WikiLink]:
    """Parse the 'links:' block from raw YAML frontmatter text."""
    links: list[WikiLink] = []
    in_links = False
    current: dict[str, str] = {}

    for line in yaml_text.split("\n"):
        stripped = line.strip()
        if stripped == "links:":
            in_links = True
            continue
        if not in_links:
            continue
        # End of links block: top-level non-indented, non-list key
        if stripped and not line.startswith(" ") and not stripped.startswith("-") and ":" in stripped:
            break
        if stripped.startswith("- target:"):
            if current.get("target") and current.get("rel"):
                links.append(WikiLink(
                    target=current["target"],
                    rel=current["rel"],
                    note=current.get("note", ""),
                ))
            current = {"target": stripped[len("- target:"):].strip().strip("\"'")}
        elif stripped.startswith("target:"):
            current["target"] = stripped[len("target:"):].strip().strip("\"'")
        elif stripped.startswith("rel:"):
            current["rel"] = stripped[len("rel:"):].strip().strip("\"'")
        elif stripped.startswith("note:"):
            current["note"] = stripped[len("note:"):].strip().strip("\"'")

    if current.get("target") and current.get("rel"):
        links.append(WikiLink(
            target=current["target"],
            rel=current["rel"],
            note=current.get("note", ""),
        ))
    return links


def stable_page_id(canonical_path: str) -> str:
    """Return a deterministic v2 page ID for a canonical path."""
    normalized = (canonical_path or "").replace("\\", "/").removesuffix(".md").strip("/")
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return f"wiki_{digest}"


def _parse_v2_governance(fm: dict) -> WikiGovernance | None:
    if int(fm.get("schema_version") or 1) != 2:
        return None
    raw_confidence = fm.get("confidence") or {}
    if isinstance(raw_confidence, str):
        raw_confidence = {"level": raw_confidence, "method": "extracted"}
    raw_sources = fm.get("provenance") or fm.get("sources") or []
    provenance: list[WikiSource] = []
    for item in raw_sources:
        if isinstance(item, dict):
            provenance.append(WikiSource.model_validate(item))
    return WikiGovernance.model_validate({
        "schema_version": 2,
        "page_id": fm.get("page_id"),
        "scope": fm.get("scope"),
        "visibility": fm.get("visibility", "internal_only"),
        "external_grounding": bool(fm.get("external_grounding", False)),
        "project_ids": fm.get("project_ids") or [],
        "markets": fm.get("markets") or [],
        "counterparties": fm.get("counterparties") or [],
        "environments": fm.get("environments") or [],
        "effective_from": fm.get("effective_from"),
        "effective_until": fm.get("effective_until"),
        "reviewed_at": fm.get("reviewed_at"),
        "reviewed_by": fm.get("reviewed_by"),
        "owner": fm.get("owner"),
        "verification_status": fm.get("verification_status", "unreviewed"),
        "canonical_path": fm.get("canonical_path"),
        "aliases": fm.get("aliases") or [],
        "superseded_by": fm.get("superseded_by"),
        "archive_reason": fm.get("archive_reason"),
        "conflict_state": fm.get("conflict_state", "none"),
        "revision": fm.get("revision", 1),
        "content_hash": fm.get("content_hash"),
        "confidence": raw_confidence,
        "provenance": [source.model_dump(mode="json", exclude_none=True) for source in provenance],
    })


def parse_page(raw: str) -> WikiPage:
    """Parse a raw Markdown string into a backward-compatible WikiPage."""
    raw = (raw or "").lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    m = _FRONTMATTER_RE.match(raw)
    if not m:
        return WikiPage(body=raw)
    yaml_text = m.group(1)
    loaded = yaml.safe_load(yaml_text) or {}
    fm = loaded if isinstance(loaded, dict) else _parse_yaml_simple(yaml_text)
    links = [
        WikiLink(
            target=str(item.get("target") or ""),
            rel=str(item.get("rel") or ""),
            note=str(item.get("note") or ""),
        )
        for item in (fm.get("links") or [])
        if isinstance(item, dict) and item.get("target") and item.get("rel")
    ]
    governance = _parse_v2_governance(fm)
    body = raw[m.end():]
    if governance:
        sources = [source.legacy_locator() for source in governance.provenance]
        confidence = governance.confidence.level
        visibility = (
            "public" if governance.visibility == WikiVisibility.EXTERNAL_SAFE
            else "internal_only"
        )
    else:
        raw_sources = fm.get("sources") or []
        sources = [str(source) for source in raw_sources if not isinstance(source, dict)]
        confidence = str(fm.get("confidence") or "")
        visibility = str(fm.get("visibility") or "public")
    def _scalar_text(value: object) -> str:
        if value is None:
            return ""
        if hasattr(value, "isoformat"):
            return str(value.isoformat())
        return str(value)

    return WikiPage(
        title=str(fm.get("title") or ""),
        type=str(fm.get("type") or ""),
        tags=[str(value) for value in (fm.get("tags") or [])],
        sources=sources,
        created=_scalar_text(fm.get("created")),
        updated=_scalar_text(fm.get("updated")),
        confidence=confidence,
        status=str(fm.get("status") or "active"),
        org_type=str(fm.get("org_type")) if fm.get("org_type") else None,
        domains=[str(value) for value in (fm.get("domains") or [])],
        visibility=visibility,
        visibility_explicit="visibility" in fm,
        governance=governance,
        links=links,
        body=body,
    )


def expected_dir_for_type(page_type: str) -> str | None:
    """Return canonical top-level directory for a page type, or None if unknown."""
    return TYPE_TO_DIR.get(page_type or "")


def path_type_dir_mismatch(path: str, page_type: str) -> str | None:
    """If path's first segment does not match page type's dir, return error string."""
    if not path or not page_type:
        return None
    expected = expected_dir_for_type(page_type)
    if not expected:
        return None
    top = path.strip("/").split("/", 1)[0]
    if top in LEGACY_PLURAL_DIRS:
        return f"path uses legacy plural dir '{top}'; use '{expected}/' instead"
    if top != expected:
        return f"path dir '{top}' does not match type '{page_type}' (expected '{expected}/')"
    return None


def extract_summary(body: str) -> str | None:
    """Extract bold one-line summary or first non-empty paragraph (≤200 chars)."""
    if not body or not body.strip():
        return None
    # Strip residual frontmatter if caller passed full file by mistake
    text = body.strip()
    m = _BOLD_SUMMARY_RE.search(text)
    if m:
        summary = m.group(1).strip()
        return summary[:200] if summary else None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("---"):
            continue
        # Strip leading list markers
        cleaned = re.sub(r"^[-*]\s+", "", stripped)
        cleaned = cleaned.strip("*").strip()
        if cleaned:
            return cleaned[:200]
    return None


def has_bold_summary(body: str) -> bool:
    """True if body starts (after whitespace) with a **bold summary** line."""
    if not body:
        return False
    for line in body.strip().splitlines():
        if not line.strip():
            continue
        return bool(_BOLD_SUMMARY_RE.match(line.strip()))
    return False


def ensure_bold_summary(body: str, title: str) -> str:
    """Prepend **title.** summary if body lacks a bold summary line."""
    if has_bold_summary(body):
        return body
    summary = (title or "Untitled").strip().rstrip(".")
    prefix = f"**{summary}.**\n\n"
    return prefix + (body or "").lstrip("\n")


def validate_sources(sources: list[str], *, strict: bool = False) -> list[str]:
    """Return validation issues for sources list.

    strict=True rejects empty and weak-only sources (for new curator writes).
    non-strict returns weak warnings as empty list for errors (caller may warn).
    """
    errors: list[str] = []
    if not sources:
        if strict:
            errors.append("sources cannot be empty")
        return errors
    normalized = [str(s).strip() for s in sources if str(s).strip()]
    if not normalized and strict:
        errors.append("sources cannot be empty")
        return errors
    if strict and normalized and all(s.lower() in _WEAK_SOURCES for s in normalized):
        errors.append(
            "sources are too weak (only agent_write/email_thread); "
            "use email:<id>, doc:<path>, operator:name@date, or rule:<id>"
        )
    return errors


def sources_are_weak(sources: list[str]) -> bool:
    """True if sources empty or every entry is a weak placeholder."""
    if not sources:
        return True
    normalized = [str(s).strip() for s in sources if str(s).strip()]
    if not normalized:
        return True
    return all(s.lower() in _WEAK_SOURCES for s in normalized)


def validate_page(page: WikiPage) -> list[str]:
    """Return a list of validation error strings (empty = valid)."""
    errors: list[str] = []
    if not page.title:
        errors.append("missing required field: title")
    if page.type and page.type not in VALID_TYPES:
        errors.append(f"invalid type '{page.type}', must be one of {sorted(VALID_TYPES)}")
    if page.confidence and page.confidence not in VALID_CONFIDENCE:
        errors.append(f"invalid confidence '{page.confidence}', must be one of {VALID_CONFIDENCE}")
    if page.status and page.status not in VALID_STATUS:
        errors.append(f"invalid status '{page.status}', must be one of {VALID_STATUS}")
    if page.org_type and page.org_type not in VALID_ORG_TYPES:
        errors.append(f"invalid org_type '{page.org_type}', must be one of {sorted(VALID_ORG_TYPES)}")
    if page.visibility and page.visibility not in VALID_VISIBILITY:
        errors.append(f"invalid visibility '{page.visibility}', must be one of {sorted(VALID_VISIBILITY)}")
    for link in page.links:
        if link.rel not in VALID_LINK_RELS:
            errors.append(f"invalid link rel '{link.rel}', must be one of {sorted(VALID_LINK_RELS)}")
    for target, rel in duplicate_wiki_link_keys(page.links):
        errors.append(f"duplicate typed edge '{target}' with rel '{rel}'")
    return errors


def _render_v2_page(page: WikiPage) -> str:
    governance = page.governance
    if governance is None:
        raise ValueError("v2 rendering requires governance metadata")
    frontmatter: dict = {
        "schema_version": 2,
        "page_id": governance.page_id,
        "title": page.title,
        "type": page.type,
        "tags": list(page.tags),
        "created": page.created or None,
        "updated": page.updated or None,
        "status": page.status,
        "scope": governance.scope.value,
        "visibility": governance.visibility.value,
        "external_grounding": governance.external_grounding,
        "project_ids": governance.project_ids,
        "markets": governance.markets,
        "counterparties": governance.counterparties,
        "environments": governance.environments,
        "domains": list(page.domains),
        "effective_from": governance.effective_from,
        "effective_until": governance.effective_until,
        "reviewed_at": governance.reviewed_at,
        "reviewed_by": governance.reviewed_by,
        "owner": governance.owner,
        "verification_status": governance.verification_status.value,
        "canonical_path": governance.canonical_path,
        "aliases": governance.aliases,
        "superseded_by": governance.superseded_by,
        "archive_reason": governance.archive_reason,
        "conflict_state": governance.conflict_state.value,
        "revision": governance.revision,
        "content_hash": governance.content_hash,
        "confidence": governance.confidence.model_dump(mode="json"),
        "provenance": [
            source.model_dump(mode="json", exclude_none=True)
            for source in governance.provenance
        ],
        "links": [
            {"target": link.target, "rel": link.rel, **({"note": link.note} if link.note else {})}
            for link in page.links
        ],
    }
    if page.org_type:
        frontmatter["org_type"] = page.org_type
    compact = {
        key: value for key, value in frontmatter.items()
        if value not in (None, [], "")
    }
    yaml_text = yaml.safe_dump(
        compact,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    ).rstrip()
    return f"---\n{yaml_text}\n---\n\n{page.body.lstrip()}"


def render_page(page: WikiPage) -> str:
    """Render a WikiPage back to Markdown with frontmatter."""
    if page.governance is not None:
        return _render_v2_page(page)

    lines = ["---"]
    lines.append(f'title: "{page.title}"')
    if page.type:
        lines.append(f"type: {page.type}")
    if page.tags:
        lines.append(f"tags: [{', '.join(page.tags)}]")
    if page.sources:
        lines.append(f"sources: [{', '.join(page.sources)}]")
    if page.created:
        lines.append(f'created: "{page.created}"')
    if page.updated:
        lines.append(f'updated: "{page.updated}"')
    if page.confidence:
        lines.append(f"confidence: {page.confidence}")
    if page.status:
        lines.append(f"status: {page.status}")
    if page.org_type:
        lines.append(f"org_type: {page.org_type}")
    if page.domains:
        lines.append(f"domains: [{', '.join(page.domains)}]")
    if page.visibility and page.visibility != "public":
        lines.append(f"visibility: {page.visibility}")
    if page.links:
        lines.append("links:")
        for link in page.links:
            lines.append(f"  - target: {link.target}")
            lines.append(f"    rel: {link.rel}")
            if link.note:
                lines.append(f'    note: "{link.note}"')
    lines.append("---")
    lines.append("")
    lines.append(page.body.lstrip("\n"))
    return "\n".join(lines)


def extract_wiki_links(text: str) -> list[str]:
    """Extract all [[wiki-link]] targets from text."""
    return _LINK_RE.findall(text)
