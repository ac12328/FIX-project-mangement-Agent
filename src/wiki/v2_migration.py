"""Read-only helpers for planning WikiPage v2 migration."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from src.wiki.models import (
    ConfidenceMethod,
    WikiConfidence,
    WikiGovernance,
    WikiScope,
    WikiSource,
    WikiVisibility,
    VerificationStatus,
)
from src.wiki.policy import scan_sensitive_text
from src.wiki.schema import WikiPage, parse_page, stable_page_id

_PROJECT_ID_RE = re.compile(r"\b[A-Z]{2,5}\d{4}(?:-\d+)?\b")
_MARKET_TAGS = {"HK", "PM", "US", "JP", "SG", "CN"}


def _source_from_legacy(locator: str) -> WikiSource | None:
    value = (locator or "").strip()
    if not value or ":" not in value:
        return None
    prefix, source_id = value.split(":", 1)
    source_type = "document" if prefix.lower() == "doc" else prefix.lower()
    if source_type not in {"email", "thread", "document", "operator", "evidence", "rule", "code"}:
        return None
    return WikiSource(source_type=source_type, source_id=source_id)


def infer_v2_governance(page: WikiPage, canonical_path: str) -> WikiGovernance:
    """Infer conservative v2 metadata without claiming factual verification."""
    body = page.body or ""
    project_ids = sorted(set(_PROJECT_ID_RE.findall(body + " " + " ".join(page.tags))))
    markets = sorted({tag.upper() for tag in page.tags if tag.upper() in _MARKET_TAGS})
    sensitive = bool(scan_sensitive_text(body, include_identifiers=True))

    if project_ids or page.type == "entity" and "project" in {tag.lower() for tag in page.tags}:
        scope = WikiScope.PROJECT
    elif page.type == "market" and markets:
        scope = WikiScope.MARKET
    elif page.type == "insight" and (project_ids or markets):
        scope = WikiScope.EXAMPLE
    elif page.type == "insight":
        scope = WikiScope.INTERNAL_RUNBOOK
    elif page.type == "playbook" and ("internal-ops" in page.domains or page.visibility == "internal_only"):
        scope = WikiScope.INTERNAL_RUNBOOK
    else:
        scope = WikiScope.GLOBAL

    if scope == WikiScope.PROJECT:
        visibility = WikiVisibility.RESTRICTED
    else:
        visibility = WikiVisibility.INTERNAL_ONLY

    provenance = [
        source for source in (_source_from_legacy(value) for value in page.sources)
        if source is not None
    ]
    confidence_level = page.confidence if page.confidence in {"high", "medium", "low"} else "low"
    content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    archive_reason = "legacy archived page pending canonical review" if page.status == "archived" else None

    return WikiGovernance(
        page_id=stable_page_id(canonical_path),
        scope=scope,
        visibility=visibility,
        external_grounding=False,
        project_ids=project_ids,
        markets=markets,
        verification_status=VerificationStatus.UNREVIEWED,
        canonical_path=canonical_path,
        archive_reason=archive_reason,
        confidence=WikiConfidence(
            level=confidence_level,
            method=ConfidenceMethod.EXTRACTED,
        ),
        provenance=provenance,
        content_hash=content_hash,
    )


def inventory_page(path: Path, wiki_dir: Path) -> dict[str, object]:
    """Return a redacted migration record for one canonical page."""
    canonical_path = path.relative_to(wiki_dir).as_posix().removesuffix(".md")
    page = parse_page(path.read_text(encoding="utf-8", errors="replace"))
    governance = page.governance or infer_v2_governance(page, canonical_path)
    findings = scan_sensitive_text(page.body or "", include_identifiers=True)
    return {
        "path": canonical_path,
        "page_id": governance.page_id,
        "source_schema_version": 2 if page.governance else 1,
        "proposed_scope": governance.scope.value,
        "proposed_visibility": governance.visibility.value,
        "project_ids": governance.project_ids,
        "markets": governance.markets,
        "verification_status": governance.verification_status.value,
        "provenance_count": len(governance.provenance),
        "sensitive_kinds": sorted({finding.kind for finding in findings}),
        "content_hash": governance.content_hash,
    }
