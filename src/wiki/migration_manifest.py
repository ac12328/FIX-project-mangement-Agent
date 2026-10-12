"""Redacted wiki migration plan generation and validation."""
from __future__ import annotations

import hashlib
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from src.wiki.corpus import CorpusPage, list_canonical_pages, list_legacy_copies
from src.wiki.duplicates import audit_archive_redirects, find_duplicate_candidates
from src.wiki.v2_migration import infer_v2_governance

_MANIFEST_VERSION = 1


def _root_hash(pages: list[CorpusPage]) -> str:
    payload = "\n".join(f"{page.path}:{page.content_hash}" for page in pages)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _batch_a_action(page: CorpusPage) -> tuple[str, str, bool]:
    governance = page.page.governance or infer_v2_governance(page.page, page.path)
    scope = governance.scope.value
    if "password" in page.sensitive_kinds or "private_key" in page.sensitive_kinds or "access_token" in page.sensitive_kinds:
        return "redact_to_pointer", "secret-like content must leave the wiki", True
    if page.sensitive_kinds and scope == "project":
        return "move_to_restricted_evidence", "project-specific identifiers belong in project evidence/configuration", True
    if page.sensitive_kinds:
        return "split_generic_from_project", "generic page contains exact identifiers or infrastructure", True
    if scope == "project":
        return "move_to_project_truth", "project snapshot/state should not remain reusable knowledge", True
    return "keep", "no Batch A action", False


def build_batch_a_manifest(wiki_dir: Path) -> dict:
    pages = list_canonical_pages(wiki_dir)
    actions: list[dict] = []
    for page in pages:
        action, rationale, review = _batch_a_action(page)
        if action == "keep":
            continue
        governance = page.page.governance or infer_v2_governance(page.page, page.path)
        actions.append({
            "path": page.path,
            "page_id": governance.page_id,
            "content_hash": page.content_hash,
            "proposed_scope": governance.scope.value,
            "proposed_visibility": governance.visibility.value,
            "action": action,
            "rationale": rationale,
            "sensitive_kinds": list(page.sensitive_kinds),
            "sources_retained": list(page.page.sources),
            "manual_review_required": review,
            "rollback": {"operation": "restore_content_hash", "content_hash": page.content_hash},
        })
    legacy = [asdict(copy) for copy in list_legacy_copies(wiki_dir)]
    return {
        "manifest_version": _MANIFEST_VERSION,
        "batch": "A",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "wiki_root_hash": _root_hash(pages),
        "summary": {"actions": len(actions), "legacy_copies": len(legacy)},
        "actions": actions,
        "legacy_copies": legacy,
    }


def build_duplicate_manifest(wiki_dir: Path) -> dict:
    """Build a redacted active-only duplicate plan plus archive redirect audit."""
    pages = list_canonical_pages(wiki_dir)
    candidates = find_duplicate_candidates(pages)
    groups = [
        {
            "group_id": hashlib.sha256(
                f"{candidate.left_path}|{candidate.right_path}".encode("utf-8")
            ).hexdigest()[:12],
            **asdict(candidate),
            "manual_review_required": True,
        }
        for candidate in candidates
    ]
    archive_checks = [asdict(check) for check in audit_archive_redirects(pages)]
    return {
        "manifest_version": _MANIFEST_VERSION,
        "batch": "duplicates_v2",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "wiki_root_hash": _root_hash(pages),
        "summary": {
            "active_candidate_pairs": len(groups),
            "merge_candidates": sum(group["action"] == "merge_into" for group in groups),
            "link_or_hub_candidates": sum(
                group["action"] == "link_or_hub_review" for group in groups
            ),
            "archive_redirect_checks": len(archive_checks),
            "invalid_archive_redirects": sum(
                check["status"] != "ok" for check in archive_checks
            ),
        },
        "groups": groups,
        "archive_redirect_checks": archive_checks,
    }


def validate_manifest(manifest: dict, wiki_dir: Path) -> list[str]:
    """Return manifest validation errors without exposing content values."""
    errors: list[str] = []
    pages = {page.path: page for page in list_canonical_pages(wiki_dir)}
    if manifest.get("manifest_version") != _MANIFEST_VERSION:
        errors.append("unsupported manifest_version")
    if manifest.get("wiki_root_hash") != _root_hash(list(pages.values())):
        errors.append("wiki_root_hash is stale")
    for action in manifest.get("actions") or []:
        page = pages.get(action.get("path"))
        if page is None:
            errors.append(f"missing page: {action.get('path')}")
            continue
        if action.get("content_hash") != page.content_hash:
            errors.append(f"stale content hash: {page.path}")
        if action.get("action") == "split_generic_from_project" and not action.get("manual_review_required"):
            errors.append(f"split action requires manual review: {page.path}")
    for group in manifest.get("groups") or []:
        for side in ("left", "right"):
            path = group.get(f"{side}_path")
            page = pages.get(path)
            if page is None:
                errors.append(f"missing duplicate page: {path}")
                continue
            if group.get(f"{side}_hash") != page.content_hash:
                errors.append(f"stale duplicate hash: {path}")
        if not group.get("manual_review_required"):
            errors.append(f"duplicate group requires manual review: {group.get('group_id')}")
    for check in manifest.get("archive_redirect_checks") or []:
        page = pages.get(check.get("path"))
        if page is None:
            errors.append(f"missing archived page: {check.get('path')}")
            continue
        if check.get("content_hash") != page.content_hash:
            errors.append(f"stale archive hash: {page.path}")
    return errors
