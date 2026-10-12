"""Ownership-safe, recoverable Wiki publication revocation."""
from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from src.db.connection import scoped_read, scoped_write
from src.repositories.wiki_publication_repo import append_history, get_publication
from src.wiki.publication.models import PublicationRecord
from src.wiki.publication.validator import safe_target
from src.wiki.schema import parse_page, render_page


def _markers(publication_id: str) -> tuple[str, str]:
    return f"<!-- wiki-publication:{publication_id}:start -->", f"<!-- wiki-publication:{publication_id}:end -->"


def _atomic_write(path: Path, content: str) -> None:
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".rollback", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content); handle.flush(); os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _refresh_and_verify(wiki_dir: Path, canonical_path: str, publication_id: str) -> None:
    from src.wiki.graph import WikiGraph
    from src.wiki.index_sync import rebuild_index
    from src.wiki.indexer import WikiIndex
    rebuild_index(wiki_dir)
    index = WikiIndex(wiki_dir=wiki_dir); index.build()
    graph = WikiGraph(wiki_dir=wiki_dir); graph.build()
    target = safe_target(wiki_dir, canonical_path)
    if target.exists() and _markers(publication_id)[0] in target.read_text(encoding="utf-8"):
        raise ValueError("revoked ownership marker remains")
    if not target.exists() and (any(c.file == target.name for c in index.chunks) or canonical_path in graph._nodes):
        raise ValueError("removed page remains authoritative in index or graph")


def rollback_publication(publication_id: str, *, wiki_dir: Path, actor_id: str, reason: str) -> PublicationRecord:
    with scoped_read() as conn:
        row = conn.execute("SELECT * FROM wiki_publications WHERE publication_id=?", (publication_id,)).fetchone()
    if not row:
        raise KeyError(publication_id)
    target = safe_target(Path(wiki_dir), row["canonical_path"])
    start, end = _markers(publication_id)
    if not target.exists():
        if row["page_hash"] and row["owned_content_hash"]:
            _refresh_and_verify(Path(wiki_dir), row["canonical_path"], publication_id)
            return _complete(row, actor_id, reason, {"recovered_after_removal": True})
        return _manual(row, "owned Wiki page missing", actor_id, reason)
    content = target.read_text(encoding="utf-8")
    if start not in content and end not in content and row["owned_content_hash"]:
        _refresh_and_verify(Path(wiki_dir), row["canonical_path"], publication_id)
        return _complete(row, actor_id, reason, {"recovered_after_section_removal": True})
    if content.count(start) != 1 or content.count(end) != 1:
        return _manual(row, "exact owned Wiki section changed or ambiguous", actor_id, reason)
    page = parse_page(content)
    before, rest = (page.body or "").split(start, 1)
    owned, after = rest.split(end, 1)
    owned_hash = hashlib.sha256(owned.strip().encode()).hexdigest()
    if owned_hash != row["owned_content_hash"]:
        return _manual(row, "exact owned Wiki content hash mismatch", actor_id, reason)
    remaining = "\n\n".join(part for part in (before.strip(), after.strip()) if part)
    with scoped_write() as conn:
        append_history(conn, publication_id=publication_id, event_type="removal_started",
                       actor_id=actor_id, reason=reason, payload={"owned_hash": owned_hash})
    if remaining:
        page.body = remaining
        page.status = "draft" if (page.status or "").lower() == "trusted" else page.status
        page.governance = None
        _atomic_write(target, render_page(page))
    else:
        target.unlink()
        try:
            dir_fd = os.open(target.parent, os.O_RDONLY)
            try: os.fsync(dir_fd)
            finally: os.close(dir_fd)
        except OSError:
            pass
    _refresh_and_verify(Path(wiki_dir), row["canonical_path"], publication_id)
    return _complete(row, actor_id, reason, {"owned_hash": owned_hash})


def _complete(row, actor_id: str, reason: str, payload: dict) -> PublicationRecord:
    with scoped_write() as conn:
        conn.execute("UPDATE wiki_publications SET status='revoked',revoked_at=datetime('now'),last_error=NULL WHERE publication_id=?", (row["publication_id"],))
        append_history(conn, publication_id=row["publication_id"], event_type="revoked",
                       actor_id=actor_id, reason=reason, payload=payload)
        result = get_publication(conn, row["publication_id"])
    assert result is not None
    return result


def _manual(row, error: str, actor_id: str, reason: str) -> PublicationRecord:
    with scoped_write() as conn:
        conn.execute("UPDATE wiki_publications SET status='manual_review',last_error=? WHERE publication_id=?", (error, row["publication_id"]))
        append_history(conn, publication_id=row["publication_id"], event_type="rollback_manual_review",
                       actor_id=actor_id, reason=reason, payload={"error": error})
        result = get_publication(conn, row["publication_id"])
    assert result is not None
    return result
