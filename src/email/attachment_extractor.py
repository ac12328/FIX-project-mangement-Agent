"""
Extract text content from email attachments (PDF, DOCX).

Provides helpers that the email processor can call to pull plain-text
from common attachment types so the content can be stored as evidence
or fed into LLM analysis.
"""

import io
import logging

logger = logging.getLogger(__name__)

SUPPORTED_TYPES: set[str] = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

_DOCX_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


def can_extract(content_type: str, filename: str) -> bool:
    """Return True if we can extract text from this attachment type."""
    ct = (content_type or "").strip().lower()
    fn = (filename or "").strip().lower()

    if ct in SUPPORTED_TYPES:
        return True

    # Fallback: guess from extension when content_type is generic
    if fn.endswith(".pdf"):
        return True
    if fn.endswith(".docx"):
        return True

    return False


def extract_text_from_bytes(
    data: bytes,
    content_type: str,
    filename: str,
) -> dict:
    """
    Extract text from raw attachment bytes.

    Returns a dict with keys:
        success      — bool
        text         — extracted text or None on failure
        filename     — the original filename
        content_type — the original content type
        size_bytes   — length of *data*
        error        — error message or None on success
    """
    MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024  # 10 MB

    result = {
        "success": False,
        "text": None,
        "filename": filename or "",
        "content_type": content_type or "",
        "size_bytes": len(data) if data else 0,
        "error": None,
    }

    if data and len(data) > MAX_ATTACHMENT_BYTES:
        result["error"] = (
            f"Attachment too large ({len(data)} bytes, max {MAX_ATTACHMENT_BYTES})"
        )
        return result

    try:
        ct = (content_type or "").strip().lower()
        fn = (filename or "").strip().lower()

        if ct == "application/pdf" or (ct == "" and fn.endswith(".pdf")):
            result["text"] = _extract_pdf(data)
        elif ct == _DOCX_TYPE or (ct == "" and fn.endswith(".docx")):
            result["text"] = _extract_docx(data)
        else:
            result["error"] = f"Unsupported content type: {content_type}"
            return result

        result["success"] = True
    except Exception as exc:
        logger.warning(
            "Attachment extraction failed for %s: %s", filename, exc
        )
        result["error"] = str(exc)

    return result


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _extract_pdf(data: bytes) -> str:
    """Extract text from PDF bytes using PyPDF2."""
    try:
        from PyPDF2 import PdfReader  # type: ignore[import-untyped]
    except ImportError:
        raise RuntimeError(
            "PyPDF2 is not installed. "
            "Install it with: pip install PyPDF2"
        )

    reader = PdfReader(io.BytesIO(data))
    pages_text = []
    for page in reader.pages:
        text = page.extract_text()
        if text:
            pages_text.append(text)
    return "\n".join(pages_text)


def _extract_docx(data: bytes) -> str:
    """Extract text from DOCX bytes using python-docx."""
    try:
        from docx import Document  # type: ignore[import-untyped]
    except ImportError:
        raise RuntimeError(
            "python-docx is not installed. "
            "Install it with: pip install python-docx"
        )

    doc = Document(io.BytesIO(data))
    paragraphs = [p.text for p in doc.paragraphs if p.text]
    return "\n".join(paragraphs)
