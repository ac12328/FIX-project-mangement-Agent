"""Wiki page management endpoints."""

import os
import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from config.settings import BASE_DIR
from src.api.schemas.common import ActionResult

router = APIRouter(tags=["wiki"])

WIKI_DIR = BASE_DIR / "wiki"


class WikiApproveRequest(BaseModel):
    path: str


@router.post("/wiki/pages/approve", response_model=ActionResult)
def approve_wiki_page(body: WikiApproveRequest):
    """Transition a wiki page from draft to active status."""
    rel_path = body.path

    # Guard against path traversal
    normalized = os.path.normpath(rel_path)
    if (
        normalized.startswith("..")
        or os.path.isabs(normalized)
        or ".." in normalized.split(os.sep)
    ):
        raise HTTPException(400, "Invalid path: directory traversal not allowed")

    target = WIKI_DIR / rel_path
    if not target.exists():
        raise HTTPException(404, f"Wiki page not found: {rel_path}")

    content = target.read_text(encoding="utf-8")

    # Check if page is currently in draft status
    if not re.search(r"(?m)^status:\s*draft\s*$", content):
        raise HTTPException(400, "Page is not in draft status")

    # Transition draft -> active
    updated = re.sub(r"(?m)^status:\s*draft", "status: active", content)
    target.write_text(updated, encoding="utf-8")

    # Invalidate wiki caches
    from src.wiki.indexer import invalidate_wiki_index
    from src.wiki.domain_index import invalidate_wiki_domain_index

    invalidate_wiki_index()
    invalidate_wiki_domain_index()

    return ActionResult(success=True, message=f"Page approved: {rel_path}")
