"""Revision-bound draft attachment endpoints."""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from pathlib import Path
from typing import NoReturn

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response

from config import defaults
from config.settings import CONFIG
from src.api.auth import CurrentActor
from src.api.deps import get_current_actor, get_read_conn, get_write_actor, get_write_scope
from src.api.routers.draft_workspace_common import (
    publish_workspace_event,
    raise_workspace_api_error,
)
from src.email.attachment_recovery import (
    AttachmentBusyError, AttachmentCleanupError, AttachmentIntent, abandon_upload_collision,
    attachment_visibility_lock,
    apply_file_change, prepare_delete, prepare_upload, mark_unresolved,
    record_committed_decision,
    recover_pending, require_reconciled,
)
from src.email.attachments import MAX_FILE_SIZE, AttachmentError, stage_attachment
from src.email.draft_workspace import (
    attachment_manifest,
    prepare_attachment_mutation,
    public_attachment,
    refresh_attachment_revision,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["attachments"])


def _handle_error(exc: Exception) -> NoReturn:
    if isinstance(exc, AttachmentBusyError):
        raise HTTPException(503, str(exc), headers={"Retry-After": "1"}) from exc
    if isinstance(exc, AttachmentError):
        raise HTTPException(422, str(exc))
    raise_workspace_api_error(exc)


def _find_attachment(draft_id: str, filename: str) -> dict:
    safe_name = Path(filename).name
    if filename != safe_name or safe_name in {"", ".", ".."}:
        raise ValueError("Invalid attachment filename")
    row = next(
        (item for item in attachment_manifest(draft_id) if item["filename"] == safe_name),
        None,
    )
    if not row:
        raise ValueError("Attachment not found")
    return row


@router.get("/drafts/{draft_id}/attachments")
def list_draft_attachments(
    draft_id: str,
    actor: CurrentActor = Depends(get_current_actor),
    conn: sqlite3.Connection = Depends(get_read_conn),
):
    del actor
    if not conn.execute("SELECT 1 FROM drafts WHERE draft_id=?", (draft_id,)).fetchone():
        raise HTTPException(404, "Draft not found")
    try:
        return [public_attachment(item) for item in attachment_manifest(draft_id)]
    except AttachmentBusyError as exc:
        _handle_error(exc)


@router.get("/drafts/{draft_id}/attachments/{filename}/download")
def download_attachment(
    draft_id: str,
    filename: str,
    actor: CurrentActor = Depends(get_current_actor),
    conn: sqlite3.Connection = Depends(get_read_conn),
):
    del actor
    if not conn.execute("SELECT 1 FROM drafts WHERE draft_id=?", (draft_id,)).fetchone():
        raise HTTPException(404, "Draft not found")
    try:
        # FileResponse reads after this endpoint returns. Snapshot under the
        # same guard as journaled mutations instead of streaming a path that
        # could be changed before the response body is opened.
        with attachment_visibility_lock(CONFIG.data_dir):
            row = _find_attachment(draft_id, filename)
            content = Path(row["path"]).read_bytes()
            disposition = FileResponse(
                row["path"], filename=row["filename"],
                media_type="application/octet-stream",
            ).headers["content-disposition"]
    except Exception as exc:
        _handle_error(exc)
    return Response(
        content=content, media_type="application/octet-stream",
        headers={"content-disposition": disposition},
    )


@router.post("/drafts/{draft_id}/attachments", status_code=201)
async def upload_attachment(
    draft_id: str,
    file: UploadFile = File(...),
    expected_revision: int | None = Form(None, ge=1),
    lease_token: str | None = Form(None),
    actor: CurrentActor = Depends(get_write_actor),
    write_scope=Depends(get_write_scope),
):
    # Never hold a threading lock across an await on the event-loop thread.
    workspace_enabled = defaults.DRAFT_WORKSPACE_ENABLED
    if workspace_enabled and expected_revision is None:
        _handle_error(AttachmentError("expected_revision is required"))
    try:
        data = await file.read(MAX_FILE_SIZE + 1)
        staged = stage_attachment(CONFIG.data_dir, file.filename or "unnamed", data)
    except Exception as exc:
        _handle_error(exc)
    try:
        with attachment_visibility_lock(CONFIG.data_dir):
            intent: AttachmentIntent | None = None
            try:
                require_reconciled()
                with write_scope() as conn:
                    # A previous committed operation may still have a sidecar. Finish it
                    # before another API mutation can reuse its filename.
                    recover_pending(conn, CONFIG.data_dir)
                    if not conn.execute("SELECT 1 FROM drafts WHERE draft_id=?", (draft_id,)).fetchone():
                        raise ValueError("Draft not found")
                    if workspace_enabled:
                        prepare_attachment_mutation(
                            conn, draft_id, expected_revision=expected_revision,
                            actor_user_id=actor.user_id, lease_token=lease_token,
                        )
                    for _ in range(10000):
                        intent = prepare_upload(
                            CONFIG.data_dir, draft_id, file.filename or "unnamed", staged,
                            len(data), hashlib.sha256(data).hexdigest(),
                        )
                        try:
                            apply_file_change(CONFIG.data_dir, intent)
                        except FileExistsError:
                            # Another writer claimed the name after selection. Keep its
                            # file untouched, retire only this unpublished intent, retry.
                            abandon_upload_collision(CONFIG.data_dir, intent)
                            intent = None
                            continue
                        break
                    else:
                        raise AttachmentError("Too many attachment name collisions")
                    item = _find_attachment(draft_id, intent.filename)
                    if workspace_enabled:
                        workspace = refresh_attachment_revision(
                            conn, draft_id, expected_revision=expected_revision,
                            actor_user_id=actor.user_id, lease_token=lease_token, commit=False,
                        )
                        response = {"attachment": public_attachment(item), "workspace": workspace}
                    else:
                        response = public_attachment(item)
                    record_committed_decision(conn, intent)
            except Exception as exc:
                if intent is not None:
                    try:
                        with write_scope() as recovery_conn:
                            recover_pending(recovery_conn, CONFIG.data_dir)
                    except Exception:
                        mark_unresolved()
                        logger.exception("Upload compensation failed; restart recovery required")
                        raise
                elif staged is not None:
                    try:
                        staged.unlink(missing_ok=True)
                    except OSError:
                        logger.exception("Failed to clean unjournaled staged upload")
                _handle_error(exc)

            try:
                with write_scope() as cleanup_conn:
                    recover_pending(cleanup_conn, CONFIG.data_dir)
            except AttachmentCleanupError:
                logger.exception("Failed to clean committed upload; restart recovery required")
            except Exception:
                mark_unresolved()
                logger.exception("Committed upload state is unresolved")
                raise
    except AttachmentBusyError as exc:
        try:
            staged.unlink(missing_ok=True)
        except OSError:
            logger.exception("Failed to clean staged upload after busy manifest")
        _handle_error(exc)
    publish_workspace_event(draft_id, "draft_attachment_added", {"filename": intent.filename})
    return response


@router.delete("/drafts/{draft_id}/attachments/{filename}")
def remove_attachment(
    draft_id: str,
    filename: str,
    expected_revision: int | None = Query(None, ge=1),
    lease_token: str | None = Query(None),
    actor: CurrentActor = Depends(get_write_actor),
    write_scope=Depends(get_write_scope),
):
    try:
        with attachment_visibility_lock(CONFIG.data_dir):
            row: dict | None = None
            intent: AttachmentIntent | None = None
            workspace_enabled = defaults.DRAFT_WORKSPACE_ENABLED
            try:
                require_reconciled()
                with write_scope() as conn:
                    # A previous committed operation may still have a sidecar. Finish it
                    # before another API mutation can reuse its filename.
                    recover_pending(conn, CONFIG.data_dir)
                    if not conn.execute("SELECT 1 FROM drafts WHERE draft_id=?", (draft_id,)).fetchone():
                        raise ValueError("Draft not found")
                    if workspace_enabled:
                        if expected_revision is None:
                            raise AttachmentError("expected_revision is required")
                        prepare_attachment_mutation(
                            conn, draft_id, expected_revision=expected_revision,
                            actor_user_id=actor.user_id, lease_token=lease_token,
                        )
                    row = _find_attachment(draft_id, filename)
                    intent = prepare_delete(
                        CONFIG.data_dir, draft_id, row["filename"], row["size"], row["sha256"]
                    )
                    apply_file_change(CONFIG.data_dir, intent)
                    if workspace_enabled:
                        workspace = refresh_attachment_revision(
                            conn, draft_id, expected_revision=expected_revision,
                            actor_user_id=actor.user_id, lease_token=lease_token, commit=False,
                        )
                        response = {"success": True, "workspace": workspace}
                    else:
                        response = {"success": True}
                    record_committed_decision(conn, intent)
            except Exception as exc:
                if intent is not None:
                    try:
                        with write_scope() as recovery_conn:
                            recover_pending(recovery_conn, CONFIG.data_dir)
                    except Exception:
                        mark_unresolved()
                        logger.exception("Delete compensation failed; restart recovery required")
                        raise
                _handle_error(exc)

            try:
                with write_scope() as cleanup_conn:
                    recover_pending(cleanup_conn, CONFIG.data_dir)
            except AttachmentCleanupError:
                logger.exception("Failed to clean committed deletion; restart recovery required")
            except Exception:
                mark_unresolved()
                logger.exception("Committed deletion state is unresolved")
                raise
    except AttachmentBusyError as exc:
        _handle_error(exc)
    publish_workspace_event(draft_id, "draft_attachment_removed", {"filename": row["filename"]})
    return response
