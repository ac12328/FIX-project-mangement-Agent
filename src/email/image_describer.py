"""
LLM vision-based image description for email attachments.

Calls the vision-capable LLM to describe screenshots and images
attached to client emails. Descriptions are stored as evidence
and injected into downstream analysis.
"""

import base64
import hashlib
import logging
from collections.abc import Callable
from pathlib import Path

from src.llm.client import llm_describe_image

logger = logging.getLogger(__name__)

_IMAGE_PROMPT = (
    "Describe this screenshot from a FIX protocol onboarding email. "
    "Focus on error messages, log entries, configuration details, "
    "session IDs, and any actionable information. "
    "Be concise — summarise in 2-4 sentences."
)


def describe_email_images(
    image_paths: list[str],
    *,
    prompt: str | None = None,
    on_result: Callable[[dict], None] | None = None,
) -> list[dict]:
    """Describe images using the vision LLM.

    Args:
        image_paths: File paths to saved image attachments.
        prompt: Custom prompt override (defaults to FIX protocol prompt).

    Returns:
        List of dicts with keys: ``filename``, ``description``, ``path``.
        Failed images are skipped. A receipt-sink error stops the batch; no retry.
        Payload hashes cover the exact bytes sent, not a later file read.
    """
    if not image_paths:
        return []

    effective_prompt = prompt or _IMAGE_PROMPT
    results: list[dict] = []

    for path in image_paths:
        try:
            p = Path(path)
            if not p.exists():
                logger.warning("Image file not found: %s", path)
                continue

            # Infer content type from extension
            ext = p.suffix.lower()
            ct_map = {
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".gif": "image/gif",
                ".webp": "image/webp",
                ".bmp": "image/bmp",
                ".tiff": "image/tiff",
                ".tif": "image/tiff",
            }
            content_type = ct_map.get(ext, "image/png")

            payload = p.read_bytes()
            b64 = base64.b64encode(payload).decode("ascii")
            description = llm_describe_image(
                "image_description",
                effective_prompt,
                b64,
                content_type=content_type,
            )

            description = description.strip()
            if not description:
                continue
            result = {
                "filename": p.name,
                "description": description,
                "path": path,
                "payload_sha256": hashlib.sha256(payload).hexdigest(),
                "prompt_sha256": hashlib.sha256(effective_prompt.encode("utf-8")).hexdigest(),
            }
            logger.info("Described image %s: %d chars", p.name, len(description))

        except Exception:
            logger.warning("Failed to describe image %s", path, exc_info=True)
            continue

        # Outside the model exception handler: stop before another paid call if
        # this locally returned output cannot be durably retained.
        if on_result is not None:
            on_result(result)
        results.append(result)

    return results
