"""Approved grounding and attachment helpers for the draft_reply tool."""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Callable, ContextManager

from config.settings import BASE_DIR

logger = logging.getLogger(__name__)

_WIKI_DIR = BASE_DIR / "wiki"
APPROVED_DRAFT_WIKI_PATHS = frozenset()
PM_UAT_CANONICAL_PATH = "playbook/conformance/public-demo-uat-guidelines"


def resolve_draft_doc_keys(
    doc_keys: list[str] | None,
    context: str,
) -> list[str]:
    """Return deduplicated explicit and context-implied reference document keys."""
    resolved: list[str] = []
    seen: set[str] = set()
    for raw_key in doc_keys or []:
        key = str(raw_key).strip()
        if key and key not in seen:
            seen.add(key)
            resolved.append(key)

    lowered = (context or "").casefold()
    if (
        any(term in lowered for term in ("conformance", "test case", "guideline"))
        and "conformance_test_plan" not in seen
    ):
        resolved.append("conformance_test_plan")
    return resolved


def attach_docs_to_draft(
    draft_id: str,
    doc_keys: list[str],
    *,
    conn: sqlite3.Connection | None = None,
    write_scope: Callable[[], ContextManager[sqlite3.Connection]] | None = None,
    actor_user_id: str = "system:draft-reply",
) -> tuple[list[dict[str, Any]], list[str]]:
    """Attach reference-library documents and return metadata plus failed keys."""
    from config.settings import CONFIG
    from src.db.connection import scoped_write
    from src.email.attachments import AttachmentError, list_attachments
    from src.email.draft_workspace import WorkspaceLockedError
    from src.email.reference_library import get_reference_doc, resolve_doc_path
    from src.services.committed_attachment_service import (
        add_committed_draft_attachment, explicit_attachment_write_scope,
    )

    if conn is not None and write_scope is not None:
        raise ValueError("Supply either an explicit connection or a write scope")
    attachment_scope = (
        (lambda: explicit_attachment_write_scope(conn)) if conn is not None
        else write_scope or scoped_write
    )
    failed: list[str] = []
    for key in doc_keys:
        doc = get_reference_doc(key)
        path = resolve_doc_path(key) if doc is not None else None
        if doc is None or path is None or not path.exists():
            logger.warning("draft_reply: unavailable doc_key '%s' for draft %s", key, draft_id)
            failed.append(key)
            continue
        try:
            add_committed_draft_attachment(
                attachment_scope, data_dir=CONFIG.data_dir, draft_id=draft_id,
                filename=doc["filename"], data=path.read_bytes(),
                actor_user_id=actor_user_id,
            )
        except (AttachmentError, OSError, ValueError, WorkspaceLockedError):
            logger.warning("draft_reply: failed to attach '%s' to draft %s", key, draft_id)
            failed.append(key)

    attached = [
        item
        for item in list_attachments(CONFIG.data_dir, draft_id)
        if item.get("filename")
    ]
    return attached, failed


def load_approved_draft_wiki(paths: list[str] | None) -> str:
    """Load complete operator-reviewed Wiki bodies from a narrow draft allowlist."""
    from src.wiki.schema import parse_page

    bodies: list[str] = []
    wiki_root = _WIKI_DIR.resolve()
    for raw_path in paths or []:
        wiki_path = str(raw_path).strip().replace("\\", "/").strip("/").removesuffix(".md")
        if wiki_path not in APPROVED_DRAFT_WIKI_PATHS:
            raise ValueError(f"Wiki page is not approved for draft grounding: {wiki_path}")
        page_path = (_WIKI_DIR / f"{wiki_path}.md").resolve()
        if wiki_root not in page_path.parents or not page_path.is_file():
            raise ValueError(f"Approved Wiki page not found: {wiki_path}")
        page = parse_page(page_path.read_text(encoding="utf-8-sig"))
        if page.status != "trusted" or page.confidence != "high":
            raise ValueError(f"Wiki page is not trusted/high confidence: {wiki_path}")
        bodies.append(f"[wiki:{wiki_path}]\n{page.body.strip()}")
    return "\n\n---\n\n".join(bodies)


def pm_uat_draft_issues(body: str) -> list[str]:
    """Fail closed: this edition supplies no approved operational UAT checklist."""
    return ["No independently reviewed operational UAT guidance is bundled"]


def pm_uat_correction(issues: list[str]) -> str:
    """Require a reviewed source rather than invent missing operational content."""
    return (
        "Do not produce operational testing instructions from the demo Wiki. "
        "Explain that approved guidance is unavailable and request an independently reviewed source. "
        + "; ".join(issues)
    )
