"""Crash-safe governed Wiki publisher with deterministic completion gates."""
from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
import time
from pathlib import Path
from typing import Callable

from src.db.connection import scoped_read, scoped_write
from src.repositories.wiki_publication_repo import (
    append_history, claim_publication, complete_claimed_status, fail_publication,
    get_publication_for_fact, insert_acknowledgement, insert_revision, set_revoke_phase,
    set_status,
)
from src.wiki.models import ConfidenceMethod, VerificationStatus, WikiConfidence, WikiGovernance, WikiScope, WikiSource, WikiVisibility
from src.wiki.publication.models import PublicationRecord
from src.wiki.publication.proposer import load_plan
from src.wiki.publication.validator import safe_target, stage_candidate_root, validate_published_page
from src.wiki.schema import WikiPage, parse_page, render_page, stable_page_id

MAX_REPAIRS = 2


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _markers(publication_id: str) -> tuple[str, str]:
    return f"<!-- wiki-publication:{publication_id}:start -->", f"<!-- wiki-publication:{publication_id}:end -->"


def _owned_section(plan, body: str | None = None) -> str:
    start, end = _markers(plan.publication_id)
    return f"{start}\n{(body if body is not None else plan.body).strip()}\n{end}"


def _render(plan, *, revision: int, existing: str = "", body: str | None = None) -> str:
    section = _owned_section(plan, body)
    start, end = _markers(plan.publication_id)
    timestamp = datetime.fromisoformat(plan.proposed_at.replace("Z", "+00:00"))
    if existing:
        count = existing.count(start)
        if count > 1 or existing.count(end) != count:
            raise ValueError("managed publication ownership markers are ambiguous")
        if count != 1:
            raise ValueError("existing page has no ownership-proven managed section")
        prior = parse_page(existing); prior_body = prior.body or ""
        body_before, rest = prior_body.split(start, 1); _, body_after = rest.split(end, 1)
        body_text = "\n\n".join(part for part in (body_before.strip(), section, body_after.strip()) if part)
        created = prior.created
    else:
        body_text = section; created = timestamp.date().isoformat()
    governance = WikiGovernance(
        schema_version=2, page_id=stable_page_id(plan.canonical_path), scope=WikiScope(plan.scope),
        visibility=WikiVisibility(plan.visibility), project_ids=plan.project_ids, markets=plan.markets,
        counterparties=plan.counterparties, canonical_path=plan.canonical_path,
        verification_status=VerificationStatus.OPERATOR_VERIFIED, reviewed_by=plan.ownership["decision_id"],
        reviewed_at=timestamp.isoformat(), revision=revision,
        content_hash=_hash((body or plan.body).strip()),
        confidence=WikiConfidence(level="high", method=ConfidenceMethod.OPERATOR_VERIFIED),
        provenance=[WikiSource(source_type="evidence", source_id=ref) for ref in plan.evidence_refs],
    )
    return render_page(WikiPage(title=plan.title, type=plan.page_type, created=created,
        updated=timestamp.date().isoformat(), status="trusted", governance=governance, body=body_text))


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".staged", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content); handle.flush(); os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)


def repair_content(content: str, issues: list[str], revision: int) -> str:
    return content


def _default_index_refresh(wiki_dir: Path) -> None:
    from src.wiki.graph import WikiGraph, invalidate_wiki_graph
    from src.wiki.index_sync import rebuild_index
    from src.wiki.indexer import WikiIndex, invalidate_wiki_index
    rebuild_index(wiki_dir); WikiIndex(wiki_dir=wiki_dir).build(); WikiGraph(wiki_dir=wiki_dir).build()
    invalidate_wiki_index(); invalidate_wiki_graph()


def _record_failure(publication_id: str, error: str, *, manual: bool = False) -> PublicationRecord:
    with scoped_write() as conn:
        set_status(conn, publication_id, "manual_review" if manual else "failed", error=error)
        row = conn.execute("SELECT fact_id FROM wiki_publications WHERE publication_id=?", (publication_id,)).fetchone()
        result = get_publication_for_fact(conn, row["fact_id"])
    assert result is not None
    return result


def _persist_success(plan, *, token: str, content: str, revision: int, repair_count: int,
                     page_hash: str, lint_issues: list[str], indexed_at: str) -> PublicationRecord:
    try:
        with scoped_write() as conn:
            claimed = conn.execute("SELECT 1 FROM wiki_publications WHERE publication_id=? AND status='publishing' AND lease_token=?",
                                   (plan.publication_id, token)).fetchone()
            if not claimed:
                raise RuntimeError("publication claim lost")
            insert_revision(conn, publication_id=plan.publication_id, revision=revision,
                repair_of_revision=revision - 1 if revision > 1 else None, content=content,
                content_hash=plan.planned_content_hash, page_hash=page_hash, lint_issues=lint_issues)
            insert_acknowledgement(conn, source_watermark=plan.source_watermark,
                publication_id=plan.publication_id, revision=revision,
                content_hash=plan.planned_content_hash, page_hash=page_hash, indexed_at=indexed_at)
            changed = conn.execute("""UPDATE wiki_publications SET status='active',page_hash=?,owned_content_hash=?,
              page_revision=?,repair_count=?,last_error=NULL,completed_at=?,lease_token=NULL,lease_expires_at=NULL
              WHERE publication_id=? AND status='publishing' AND lease_token=?""",
              (page_hash, plan.planned_content_hash, revision, repair_count, indexed_at,
               plan.publication_id, token))
            if changed.rowcount != 1:
                raise RuntimeError("publication claim lost")
            append_history(conn, publication_id=plan.publication_id, event_type="completed",
                           actor_id="wiki-publication-worker", reason="all completion gates passed",
                           payload={"revision": revision, "page_hash": page_hash})
            result = get_publication_for_fact(conn, plan.fact_id)
    except (sqlite3.IntegrityError, ValueError) as exc:
        complete_claimed_status(plan.publication_id, token, status="manual_review", error=str(exc))
        with scoped_read() as conn:
            result = get_publication_for_fact(conn, plan.fact_id)
    assert result is not None
    return result


def _publish_revoke(plan, token: str, wiki_dir: Path,
                    index_refresh: Callable[[Path], None] | None) -> PublicationRecord:
    from src.wiki.publication.rollback import rollback_publication
    with scoped_read() as conn:
        current = conn.execute("SELECT revoke_phase FROM wiki_publications WHERE publication_id=?",
                               (plan.publication_id,)).fetchone()
        owner = conn.execute("""SELECT publication_id FROM wiki_publications
          WHERE canonical_path=? AND operation='upsert' AND status IN ('active','revoked')
          ORDER BY completed_at DESC LIMIT 1""", (plan.canonical_path,)).fetchone()
    phase = current["revoke_phase"] if current else None
    if phase is None:
        with scoped_write() as conn:
            set_revoke_phase(conn, publication_id=plan.publication_id, token=token, phase="claimed",
                             actor_id="wiki-publication-worker")
        phase = "claimed"
    if not owner:
        complete_claimed_status(plan.publication_id, token, status="manual_review",
                                error="revoke has no owned publication history")
    elif phase == "claimed":
        removed = rollback_publication(owner["publication_id"], wiki_dir=wiki_dir,
                                       actor_id=plan.decision_id, reason="approved revoke publication")
        if removed.status != "revoked":
            complete_claimed_status(plan.publication_id, token, status="manual_review",
                                    error=removed.last_error or "revoke failed")
        else:
            with scoped_write() as conn:
                set_revoke_phase(conn, publication_id=plan.publication_id, token=token,
                                 phase="authority_removed", actor_id="wiki-publication-worker",
                                 payload={"owner_publication_id": owner["publication_id"]})
            phase = "authority_removed"
    if phase == "authority_removed":
        try:
            (index_refresh or _default_index_refresh)(wiki_dir)
        except Exception as exc:
            complete_claimed_status(plan.publication_id, token, status="failed", error=str(exc))
            raise
        with scoped_write() as conn:
            set_revoke_phase(conn, publication_id=plan.publication_id, token=token,
                             phase="index_verified", actor_id="wiki-publication-worker")
        phase = "index_verified"
    indexed_at = plan.proposed_at
    page_hash = _hash("revoked:" + plan.canonical_path)
    if phase == "index_verified":
        try:
            with scoped_write() as conn:
                insert_acknowledgement(conn, source_watermark=plan.source_watermark,
                    publication_id=plan.publication_id, revision=1,
                    content_hash=plan.planned_content_hash, page_hash=page_hash, indexed_at=indexed_at)
                set_revoke_phase(conn, publication_id=plan.publication_id, token=token,
                                 phase="receipt_committed", actor_id="wiki-publication-worker")
                changed = conn.execute("""UPDATE wiki_publications SET status='revoked',page_revision=1,
                  owned_content_hash=?,page_hash=?,completed_at=?,revoked_at=?,lease_token=NULL,
                  lease_expires_at=NULL WHERE publication_id=? AND status='publishing' AND lease_token=?""",
                  (plan.planned_content_hash, page_hash, indexed_at, indexed_at,
                   plan.publication_id, token))
                if changed.rowcount != 1:
                    raise RuntimeError("publication claim lost")
        except Exception as exc:
            complete_claimed_status(plan.publication_id, token, status="failed", error=str(exc))
            raise
    with scoped_read() as conn:
        result = get_publication_for_fact(conn, plan.fact_id)
    assert result is not None
    return result


def publish_fact(fact_id: str, *, wiki_dir: Path,
                 index_refresh: Callable[[Path], None] | None = None,
                 now: datetime | None = None) -> PublicationRecord:
    plan = load_plan(fact_id); wiki_dir = Path(wiki_dir)
    token = None
    for _ in range(100):
        with scoped_write() as conn:
            token = claim_publication(conn, plan.publication_id, now=now)
            row = conn.execute("SELECT status,page_revision,page_hash FROM wiki_publications WHERE fact_id=?",
                               (fact_id,)).fetchone()
        if token or row["status"] in {"active", "revoked", "manual_review", "dead_letter"}:
            break
        time.sleep(0.01)
    if not token:
        with scoped_read() as conn:
            result = get_publication_for_fact(conn, fact_id)
        assert result is not None
        return result

    staged_root: Path | None = None
    try:
        if plan.operation == "revoke":
            return _publish_revoke(plan, token, wiki_dir, index_refresh)
        target = safe_target(wiki_dir, plan.canonical_path)
        existing = target.read_text(encoding="utf-8") if target.exists() else ""
        revision = max(int(row["page_revision"] or 0) + 1, 1)
        content = _render(plan, revision=revision, existing=existing)
        issues: list[str] = []
        staged_root = stage_candidate_root(wiki_dir)
        for repair_count in range(MAX_REPAIRS + 1):
            staged_target = safe_target(staged_root, plan.canonical_path)
            _atomic_write(staged_target, content)
            issues = validate_published_page(staged_target, expected_path=plan.canonical_path,
                                             expected_hash=parse_page(content).governance.content_hash)
            page_hash = _hash(staged_target.read_text(encoding="utf-8"))
            if not issues:
                break
            with scoped_write() as conn:
                insert_revision(conn, publication_id=plan.publication_id, revision=revision,
                    repair_of_revision=revision - 1 if revision > 1 else None, content=content,
                    content_hash=_hash(content), page_hash=page_hash, lint_issues=issues)
            if repair_count == MAX_REPAIRS:
                complete_claimed_status(plan.publication_id, token, status="manual_review",
                                        error="; ".join(issues))
                with scoped_read() as conn:
                    result = get_publication_for_fact(conn, fact_id)
                assert result is not None
                return result
            revision += 1
            content = _render(plan, revision=revision, existing=content,
                              body=repair_content(plan.body, issues, revision))
        _atomic_write(target, content)
        (index_refresh or _default_index_refresh)(wiki_dir)
        verified = target.read_text(encoding="utf-8"); page_hash = _hash(verified)
        if verified != content:
            raise ValueError("read-after-write page hash mismatch")
        return _persist_success(plan, token=token, content=verified, revision=revision,
            repair_count=revision - 1, page_hash=page_hash, lint_issues=issues,
            indexed_at=plan.proposed_at)
    except Exception as exc:
        fail_publication(plan.publication_id, token, error=str(exc))
        raise
    finally:
        if staged_root is not None:
            shutil.rmtree(staged_root, ignore_errors=True)
