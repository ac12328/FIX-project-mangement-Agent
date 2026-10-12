"""
Disk storage for email image attachments.

Saves images under ``data/attachments/{email_id}/`` and provides helpers
to load them back as base64 for LLM vision calls.
"""

import base64
import hashlib
import logging
import re
from pathlib import Path

from src.email.inbox_fetcher import AttachmentData

logger = logging.getLogger(__name__)

ATTACHMENT_DIR = Path("data/attachments")


def _sanitize_email_id(email_id: str) -> str:
    """Make an email Message-ID safe for use as a directory name."""
    return re.sub(r"[<>@/\\:\*\?\"\|]", "_", email_id)[:120]


def save_email_images(
    email_id: str,
    attachments: list[AttachmentData],
) -> list[str]:
    """Persist image attachments to disk.

    Returns a list of file paths (as strings) for the saved images.
    """
    if not attachments:
        return []

    from config import settings
    from src.email.source_capture import _retain_exact

    safe_id = _sanitize_email_id(email_id) + "_" + hashlib.sha256(email_id.encode()).hexdigest()[:16]
    root = (Path(settings.DATA_DIR) / "attachments"
            if ATTACHMENT_DIR == Path("data/attachments") else ATTACHMENT_DIR)
    target_dir = root / safe_id

    paths: list[str] = []
    for i, att in enumerate(attachments):
        # Deduplicate filenames with index prefix
        basename = re.split(r"[/\\]", att.filename or "image.bin")[-1]
        basename = re.sub(r'[^a-zA-Z0-9._-]', "_", basename)[-160:] or "image.bin"
        filename = f"{i}_{hashlib.sha256(att.data).hexdigest()}_{basename}"
        filepath = target_dir / filename
        _retain_exact(filepath.absolute(), att.data)
        paths.append(str(filepath))
        logger.debug("Saved image attachment: %s (%d bytes)", filepath, att.size_bytes)

    return paths


def load_image_as_base64(path: str) -> str:
    """Read an image file and return its base64-encoded content."""
    data = Path(path).read_bytes()
    return base64.b64encode(data).decode("ascii")
