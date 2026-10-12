"""WikiWriteGuard — validates agent-written wiki pages before committing to disk."""
from __future__ import annotations

from src.wiki.models import VerificationStatus, WikiScope, WikiVisibility
from src.wiki.policy import scan_sensitive_text
from src.wiki.schema import (
    WikiPage,
    has_bold_summary,
    path_type_dir_mismatch,
    validate_page,
    validate_sources,
)


class WikiValidationError(Exception):
    """Raised when a wiki page fails schema validation."""


class WikiWriteGuard:
    """Validates wiki pages before agent/curator writes them to disk.

    Uses schema.validate_page() for field-level checks, then guard-level checks.
    """

    def validate(
        self,
        page: WikiPage,
        *,
        path: str,
        require_summary: bool = False,
        require_sources: bool = False,
        strict_sources: bool = False,
        check_type_dir: bool = False,
    ) -> None:
        """Raise WikiValidationError if the page is invalid.

        Args:
            page: The WikiPage to validate.
            path: Target path e.g. "insight/test" or "concept/b2b/foo".
            require_summary: Require bold one-line summary (new creates).
            require_sources: Require non-empty sources.
            strict_sources: Reject weak-only sources (agent_write alone).
            check_type_dir: Enforce type ↔ directory mapping.
        """
        errors = validate_page(page)

        if not page.body or not page.body.strip():
            errors.append("body cannot be empty")

        if require_summary and not has_bold_summary(page.body or ""):
            errors.append(
                "body must start with a bold one-sentence summary "
                "(e.g. **What this is and why it matters.**)"
            )

        if require_sources and not (page.sources and any(str(s).strip() for s in page.sources)):
            errors.append("sources cannot be empty")
        if strict_sources:
            errors.extend(validate_sources(list(page.sources or []), strict=True))

        if check_type_dir and page.type:
            mismatch = path_type_dir_mismatch(path, page.type)
            if mismatch:
                errors.append(mismatch)

        if page.governance is not None:
            governance = page.governance
            canonical_path = path.strip("/").removesuffix(".md")
            if governance.canonical_path != canonical_path:
                errors.append("canonical_path must match the target path")
            if scan_sensitive_text(page.body or ""):
                errors.append("wiki body contains secret-like content")
            if governance.scope == WikiScope.PROJECT:
                if governance.visibility != WikiVisibility.RESTRICTED:
                    errors.append("project scope must use restricted visibility")
                if governance.external_grounding:
                    errors.append("project scope cannot enable external grounding")
            if governance.scope == WikiScope.EXAMPLE and governance.external_grounding:
                errors.append("example scope cannot enable external grounding")
            if governance.visibility == WikiVisibility.EXTERNAL_SAFE:
                if not governance.external_grounding:
                    errors.append("external_safe pages must explicitly enable external_grounding")
                if scan_sensitive_text(page.body or "", include_identifiers=True):
                    errors.append("external_safe page contains sensitive identifiers")
            if page.status == "trusted":
                if governance.verification_status != VerificationStatus.OPERATOR_VERIFIED:
                    errors.append("trusted v2 pages require operator verification")
                if not governance.provenance:
                    errors.append("trusted v2 pages require structured provenance")
            if page.status == "archived" and not (
                governance.superseded_by or governance.archive_reason
            ):
                errors.append("archived v2 pages require superseded_by or archive_reason")

        if errors:
            raise WikiValidationError(
                f"Wiki page {path!r} failed validation: {'; '.join(errors)}"
            )

    def check_contradictions(self, page: WikiPage) -> list[str]:
        """Return target paths from 'contradicts' links on this page."""
        return [lnk.target for lnk in page.links if lnk.rel == "contradicts"]
