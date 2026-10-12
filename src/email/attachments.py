"""Attachment storage — local file-based storage for draft email attachments."""

import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = frozenset({
    ".pdf", ".docx", ".xlsx", ".csv", ".txt",
    ".png", ".jpg", ".xml", ".cfg", ".log", ".zip",
})
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB per file
MAX_TOTAL_SIZE = 25 * 1024 * 1024  # 25MB per draft


class AttachmentError(Exception):
    pass


def validate_attachment(filename: str, data: bytes) -> tuple[bool, str | None]:
    """Validate filename extension and size."""
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        return False, f"Extension '{ext}' not allowed. Allowed: {sorted(ALLOWED_EXTENSIONS)}"
    if len(data) > MAX_FILE_SIZE:
        return False, f"File too large ({len(data)} bytes, max {MAX_FILE_SIZE})"
    return True, None


def list_attachments(base_dir: str, draft_id: str) -> list[dict]:
    """List all attachments for a draft."""
    from src.email.attachment_recovery import attachment_visibility_lock, require_reconciled

    with attachment_visibility_lock(base_dir):
        require_reconciled()
        draft_dir = Path(base_dir) / "attachments" / draft_id
        if not draft_dir.exists():
            return []
        return [
            {"filename": f.name, "size": f.stat().st_size, "path": str(f)}
            for f in sorted(draft_dir.iterdir()) if f.is_file()
        ]


def snapshot_attachments(base_dir: str, draft_id: str) -> list[dict]:
    """Copy a committed manifest's bytes before releasing the visibility guard."""
    from src.email.attachment_recovery import attachment_visibility_lock

    with attachment_visibility_lock(base_dir):
        return [
            {**item, "content": Path(item["path"]).read_bytes()}
            for item in list_attachments(base_dir, draft_id)
        ]


def stage_attachment(base_dir: str, filename: str, data: bytes) -> Path:
    """Write a validated upload outside the visible per-draft manifest."""
    valid, reason = validate_attachment(filename, data)
    if not valid:
        raise AttachmentError(reason)
    staging_dir = Path(base_dir) / "attachments" / ".staging"
    staging_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=staging_dir, delete=False) as handle:
        staged = Path(handle.name)
        try:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            # The open handle must close before Windows permits removal.
            handle.close()
            staged.unlink(missing_ok=True)
            raise
    return staged
