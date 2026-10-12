"""Reference document library — static catalog of standing reference docs."""

from pathlib import Path

from config.settings import BASE_DIR

_REFERENCE_DIR = BASE_DIR / "data" / "reference_docs"

# No private reference attachments or unverified specifications are distributed.
_CATALOG: dict[str, dict] = {}


def list_reference_docs() -> list[dict]:
    """Return all reference documents as a list of metadata dicts."""
    return [
        {"key": v["key"], "filename": v["filename"], "description": v["description"]}
        for v in _CATALOG.values()
    ]


def get_reference_doc(key: str) -> dict | None:
    """Return metadata for a single doc_key, or None if unknown."""
    entry = _CATALOG.get(key)
    return dict(entry) if entry is not None else None


def resolve_doc_path(key: str) -> Path | None:
    """Return absolute Path to the document file, or None if key unknown."""
    entry = _CATALOG.get(key)
    if entry is None:
        return None
    return _REFERENCE_DIR / entry["subfolder"] / entry["filename"]


_MAX_REFERENCE_BYTES = 5 * 1024 * 1024
_DEFAULT_READ_CHARS = 12_000
_BINARY_EXTENSIONS = frozenset({".xls", ".xlsx", ".xlsm"})


def read_reference_doc_text(key: str, *, max_chars: int = _DEFAULT_READ_CHARS) -> dict:
    """Read allowlisted reference doc text for agent on-demand loading."""
    entry = get_reference_doc(key)
    if entry is None:
        return {
            "ok": False,
            "error": f"Unknown doc_key '{key}'. Call list_reference_docs first.",
        }

    path = resolve_doc_path(key)
    if path is None or not path.exists():
        return {
            "ok": False,
            "doc_key": key,
            "filename": entry["filename"],
            "description": entry["description"],
            "error": "File not found on disk. Check data/reference_docs/.",
        }

    suffix = path.suffix.lower()
    if suffix in _BINARY_EXTENSIONS:
        return {
            "ok": True,
            "doc_key": key,
            "filename": entry["filename"],
            "description": entry["description"],
            "format": suffix.lstrip("."),
            "text": (
                f"[Binary reference document — {entry['filename']}]\n"
                f"{entry['description']}\n"
                "Attach via attach_reference_doc when drafting email."
            )[:max_chars],
            "note": "Spreadsheet/binary format — catalog description only.",
        }

    try:
        raw = path.read_bytes()
    except OSError as exc:
        return {"ok": False, "doc_key": key, "error": str(exc)}

    if len(raw) > _MAX_REFERENCE_BYTES:
        return {
            "ok": False,
            "doc_key": key,
            "error": f"File too large ({len(raw)} bytes, max {_MAX_REFERENCE_BYTES}).",
        }

    text: str | None = None
    if suffix in {".pdf", ".docx"}:
        from src.email.attachment_extractor import extract_text_from_bytes
        mime = "application/pdf" if suffix == ".pdf" else (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
        extracted = extract_text_from_bytes(raw, mime, entry["filename"])
        if extracted.get("success") and extracted.get("text"):
            text = extracted["text"]
        elif extracted.get("error"):
            return {"ok": False, "doc_key": key, "error": extracted["error"]}
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("utf-8", errors="replace")

    if not text:
        return {
            "ok": False,
            "doc_key": key,
            "error": "No extractable text in reference document.",
        }

    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars] + "\n[truncated — increase max_chars if needed]"

    return {
        "ok": True,
        "doc_key": key,
        "filename": entry["filename"],
        "description": entry["description"],
        "text": text,
        "truncated": truncated,
        "char_count": len(text),
    }
