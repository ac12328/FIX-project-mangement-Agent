"""Hash-gated application and rollback for reviewed wiki corpus migrations."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from config.settings import BASE_DIR
from src.db.connection import scoped_write
from src.wiki.corpus import list_canonical_pages
from src.wiki.index_sync import append_log, rebuild_index
from src.wiki.models import (
    ConfidenceMethod,
    VerificationStatus,
    WikiConfidence,
    WikiGovernance,
    WikiScope,
    WikiVisibility,
)
from src.wiki.paths import invalidate_wiki_caches
from src.wiki.policy import scan_sensitive_text
from src.wiki.schema import parse_page, render_page, stable_page_id
from src.wiki.v2_migration import infer_v2_governance

_BACKUP_ROOT = BASE_DIR / "data" / "wiki_migration_backups"
_SECTION_HEADING = re.compile(r"(?m)^#{1,6}\s+(.+?)\s*$")
_LIVE_HEADING = re.compile(
    r"(?i)\b(active|current|status|activity|project|test cases?|session|settings?|"
    r"configuration|connectivity|contacts?|implementation|observations?|example)\b"
)
_IDENTIFIER_LINE = re.compile(
    r"(?i)\b(host|port|sender(?:comp)?id|target(?:comp)?id|tag\s*(?:1|11)|"
    r"account(?:\s+code)?|ip|vpn|vlan|acl|network|endpoint|username|password|"
    r"session\s+status|current\s+milestone|ball\s+with|next\s+action)\b"
)


@dataclass(frozen=True)
class MigrationResult:
    result_manifest: dict
    result_path: Path | None = None


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _root_hash(wiki_dir: Path) -> str:
    pages = list_canonical_pages(wiki_dir)
    payload = "\n".join(f"{page.path}:{page.content_hash}" for page in pages)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _run_id(manifest: dict) -> str:
    payload = f"{manifest.get('wiki_root_hash')}|{manifest.get('manifest_version')}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _evidence_id(run_id: str, path: str, project_id: str) -> str:
    digest = hashlib.sha256(f"{run_id}|{path}|{project_id}".encode()).hexdigest()[:24]
    return f"wiki_migration_{digest}"


def _redact_body(body: str) -> tuple[str, int]:
    """Remove sensitive/live sections and exact identifier lines conservatively."""
    lines = (body or "").splitlines()
    kept: list[str] = []
    removed = 0
    skip_level: int | None = None
    for line in lines:
        heading = _SECTION_HEADING.match(line)
        if heading:
            level = len(line) - len(line.lstrip("#"))
            if skip_level is not None and level <= skip_level:
                skip_level = None
            if _LIVE_HEADING.search(heading.group(1)):
                skip_level = level
                removed += 1
                continue
        if skip_level is not None:
            removed += 1
            continue
        if _IDENTIFIER_LINE.search(line) or scan_sensitive_text(line, include_identifiers=True):
            removed += 1
            continue
        kept.append(line)
    text = "\n".join(kept).strip()
    if not text:
        text = "**Restricted project-specific material was migrated to approved evidence storage.**"
    return text + "\n", removed


def _base_governance(page, path: str) -> WikiGovernance:
    if page.governance is not None:
        return page.governance
    try:
        return infer_v2_governance(page, path)
    except ValueError:
        return WikiGovernance(
            page_id=stable_page_id(path),
            scope=WikiScope.INTERNAL_RUNBOOK,
            visibility=WikiVisibility.INTERNAL_ONLY,
            canonical_path=path,
            confidence=WikiConfidence(
                level=page.confidence if page.confidence in {"high", "medium", "low"} else "low",
                method=ConfidenceMethod.EXTRACTED,
            ),
        )


def _archive_stub(page, path: str, evidence_ids: list[str]):
    page.status = "archived"
    page.body = (
        "**Archived — project-specific material was migrated to restricted evidence.**\n\n"
        "This page no longer participates in reusable knowledge retrieval. "
        f"Migration references: {', '.join(evidence_ids)}.\n"
    )
    governance = _base_governance(page, path)
    governance.scope = WikiScope.INTERNAL_RUNBOOK
    governance.visibility = WikiVisibility.RESTRICTED
    governance.external_grounding = False
    governance.project_ids = []
    governance.archive_reason = "project-specific material migrated to restricted evidence"
    governance.verification_status = VerificationStatus.REVIEWED
    governance.reviewed_by = "wp6-corpus-migration"
    governance.reviewed_at = datetime.now(timezone.utc).isoformat()
    governance.revision += 1
    governance.content_hash = hashlib.sha256(page.body.encode()).hexdigest()
    page.governance = governance
    return page


def _retain_page(page, path: str, *, entity: bool = False):
    page.body, _ = _redact_body(page.body)
    governance = _base_governance(page, path)
    governance.scope = WikiScope.COUNTERPARTY if entity else WikiScope.GLOBAL
    governance.visibility = WikiVisibility.INTERNAL_ONLY
    governance.external_grounding = False
    governance.project_ids = []
    governance.counterparties = [page.title] if entity and page.title else []
    governance.verification_status = VerificationStatus.REVIEWED
    governance.reviewed_by = "wp6-corpus-migration"
    governance.reviewed_at = datetime.now(timezone.utc).isoformat()
    governance.revision += 1
    governance.confidence = WikiConfidence(
        level=page.confidence if page.confidence in {"high", "medium", "low"} else "low",
        method=ConfidenceMethod.EXTRACTED,
    )
    governance.content_hash = hashlib.sha256(page.body.encode()).hexdigest()
    page.governance = governance
    return page


def _validate_manifest(manifest: dict, wiki_dir: Path) -> list[str]:
    errors: list[str] = []
    if manifest.get("manifest_version") != 2:
        errors.append("unsupported manifest_version")
    if manifest.get("wiki_root_hash") != _root_hash(wiki_dir):
        errors.append("wiki_root_hash is stale")
    pages = {page.path: page for page in list_canonical_pages(wiki_dir)}
    for action in manifest.get("actions") or []:
        page = pages.get(action.get("path"))
        if page is None:
            errors.append(f"missing page: {action.get('path')}")
        elif page.content_hash != action.get("content_hash"):
            errors.append(f"stale content hash: {page.path}")
    for item in manifest.get("legacy_copies") or []:
        path = wiki_dir / item.get("path", "")
        if not path.is_file():
            errors.append(f"missing legacy file: {item.get('path')}")
        elif hashlib.sha256(
            path.read_text(encoding="utf-8", errors="replace").encode("utf-8")
        ).hexdigest() != item.get("content_hash"):
            errors.append(f"stale legacy hash: {item.get('path')}")
    return errors


def _backup_files(run_id: str, wiki_dir: Path, relative_paths: list[str]) -> Path:
    root = _BACKUP_ROOT / run_id
    if root.exists():
        shutil.rmtree(root)
    for rel in relative_paths:
        source = wiki_dir / rel
        if source.is_file():
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    return root


def _insert_evidence(conn, *, evidence_id: str, project_id: str, path: str, text: str, sources: list[str]):
    if conn.execute("SELECT 1 FROM evidence_store WHERE evidence_id = ?", (evidence_id,)).fetchone():
        return False
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO evidence_store
           (evidence_id, project_id, evidence_type, content_summary,
            content_text, source_ref, evidence_at, created_at)
           VALUES (?, ?, 'manual_note', ?, ?, ?, ?, ?)""",
        (
            evidence_id,
            project_id,
            f"Historical wiki material migrated from {path}",
            text,
            ";".join(sources) or f"wiki:{path}",
            now,
            now,
        ),
    )
    return True


def apply_manifest(
    manifest: dict,
    *,
    wiki_dir: Path,
    apply: bool = False,
    result_path: Path | None = None,
) -> MigrationResult:
    """Dry-run or apply a reviewed migration manifest."""
    errors = _validate_manifest(manifest, wiki_dir)
    if errors:
        raise ValueError("; ".join(errors))
    run_id = _run_id(manifest)
    actions = manifest.get("actions") or []
    legacy = manifest.get("legacy_copies") or []
    relative_paths = (
        [f"{a['path']}.md" for a in actions]
        + [i["path"] for i in legacy]
        + [f"{i['target_path']}.md" for i in legacy]
    )
    result = {
        "result_version": 1,
        "run_id": run_id,
        "source_root_hash": manifest["wiki_root_hash"],
        "mode": "apply" if apply else "dry_run",
        "operations": [],
        "evidence_ids": [],
        "legacy_removed": [],
        "unresolved": [],
    }
    if not apply:
        for action in actions:
            result["operations"].append({
                "path": action["path"],
                "decision": action["decision"],
                "destination_project_ids": action["destination_project_ids"],
                "expected_hash": action["content_hash"],
            })
        return MigrationResult(result)

    backup_root = _backup_files(run_id, wiki_dir, relative_paths + ["index.md", "log.md"])
    written: list[Path] = []
    try:
        with scoped_write() as conn:
            for action in actions:
                rel = f"{action['path']}.md"
                file_path = wiki_dir / rel
                raw = file_path.read_text(encoding="utf-8")
                page = parse_page(raw)
                evidence_ids: list[str] = []
                for project_id in action["destination_project_ids"]:
                    evidence_id = _evidence_id(run_id, action["path"], project_id)
                    _insert_evidence(
                        conn,
                        evidence_id=evidence_id,
                        project_id=project_id,
                        path=action["path"],
                        text=raw,
                        sources=action.get("sources_retained") or [],
                    )
                    evidence_ids.append(evidence_id)
                if action.get("destination_resolution") == "quarantine":
                    result["unresolved"].append(action["path"])
                if action["decision"] in {"archive_project_snapshot", "move_full_page_to_restricted_evidence"}:
                    page = _archive_stub(page, action["path"], evidence_ids)
                else:
                    page = _retain_page(
                        page,
                        action["path"],
                        entity=action["decision"] == "retain_entity_extract_project_state",
                    )
                rendered = render_page(page)
                if scan_sensitive_text(rendered, include_identifiers=True):
                    raise ValueError(f"sensitive content remains after migration: {action['path']}")
                file_path.write_text(rendered, encoding="utf-8")
                written.append(file_path)
                result["evidence_ids"].extend(evidence_ids)
                result["operations"].append({
                    "path": action["path"],
                    "before_hash": action["content_hash"],
                    "after_hash": _hash_bytes(file_path.read_bytes()),
                    "decision": action["decision"],
                    "evidence_ids": evidence_ids,
                })

            for item in legacy:
                source = wiki_dir / item["path"]
                target = wiki_dir / f"{item['target_path']}.md"
                if not target.exists():
                    page = parse_page(source.read_text(encoding="utf-8", errors="replace"))
                    page = _retain_page(page, item["target_path"], entity=item["target_path"].startswith("entity/"))
                    rendered = render_page(page)
                    if scan_sensitive_text(rendered, include_identifiers=True):
                        raise ValueError(f"sensitive legacy content remains: {item['path']}")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(rendered, encoding="utf-8")
                    written.append(target)
                source.unlink()
                result["legacy_removed"].append({
                    "path": item["path"],
                    "before_hash": item["content_hash"],
                    "target_path": item["target_path"],
                    "target_hash": _hash_bytes(target.read_bytes()),
                })

        rebuild_index(wiki_dir)
        append_log(wiki_dir, f"corpus migration {run_id}: Batch A/B + legacy", writer="wp6")
        invalidate_wiki_caches()
    except Exception:
        for rel in relative_paths + ["index.md", "log.md"]:
            backup = backup_root / rel
            target = wiki_dir / rel
            if backup.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup, target)
            elif target in written or rel in {i["path"] for i in legacy}:
                target.unlink(missing_ok=True)
        invalidate_wiki_caches()
        raise

    result["result_root_hash"] = _root_hash(wiki_dir)
    result["backup_path"] = str(backup_root)
    if result_path:
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return MigrationResult(result, result_path)


def rollback_result(result: dict, *, wiki_dir: Path) -> MigrationResult:
    """Rollback a completed migration when current hashes still match its result."""
    backup_root = Path(result["backup_path"])
    for operation in result.get("operations") or []:
        current = wiki_dir / f"{operation['path']}.md"
        if not current.is_file() or _hash_bytes(current.read_bytes()) != operation.get("after_hash"):
            raise ValueError(f"stale rollback target: {operation['path']}")
    for item in result.get("legacy_removed") or []:
        target = wiki_dir / f"{item['target_path']}.md"
        if target.is_file() and _hash_bytes(target.read_bytes()) != item.get("target_hash"):
            raise ValueError(f"stale legacy rollback target: {item['target_path']}")

    with scoped_write() as conn:
        for evidence_id in result.get("evidence_ids") or []:
            conn.execute("DELETE FROM evidence_store WHERE evidence_id = ?", (evidence_id,))
    for backup in sorted(backup_root.rglob("*")):
        if not backup.is_file():
            continue
        rel = backup.relative_to(backup_root)
        target = wiki_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backup, target)
    for operation in result.get("operations") or []:
        if not (backup_root / f"{operation['path']}.md").exists():
            (wiki_dir / f"{operation['path']}.md").unlink(missing_ok=True)
    rebuild_index(wiki_dir)
    invalidate_wiki_caches()
    rolled = dict(result)
    rolled["mode"] = "rollback"
    return MigrationResult(rolled)
