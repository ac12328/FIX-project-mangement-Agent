"""Content-addressed local source retention; never reprocess or attest completion."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.email.source_coverage import EmailSourceCoverage

MAX_RAW_SOURCE_BYTES = 30 * 1024 * 1024


class SourceCaptureReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1] = 1
    source_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    retention: Literal["retained", "unknown"]
    raw_ref: str | None = None
    coverage: EmailSourceCoverage | None = None

    @model_validator(mode="after")
    def consistent(self):
        if self.retention == "unknown":
            if self.raw_ref is not None or self.coverage is not None:
                raise ValueError("Unknown source retention cannot carry certified original coverage")
        elif self.coverage is None or self.raw_ref != f"{self.source_key}/{self.coverage.raw_sha256}/source.eml":
            raise ValueError("Retained source requires its exact content-addressed coverage reference")
        return self


def source_key(*, email_id: str, source_type: str, account_key: str | None,
               mailbox: str | None, uid_validity: int | None, source_uid: int | None) -> str:
    value = [source_type, account_key, mailbox, uid_validity, source_uid, email_id]
    return hashlib.sha256(json.dumps(value, separators=(",", ":")).encode()).hexdigest()


def _root() -> Path:
    from config import settings

    return Path(settings.DATA_DIR).absolute() / "attachments" / "source-originals"


def _safe_tree(path: Path) -> None:
    """Refuse symlink/junction/reparse ancestors, including the configured data root."""
    for item in (path, *path.parents):
        if item.is_symlink():
            raise ValueError("Source retention refuses linked paths")
        if item.exists() and getattr(item.stat(follow_symlinks=False), "st_file_attributes", 0) & 0x400:
            raise ValueError("Source retention refuses reparse paths")


def _retain_exact(path: Path, data: bytes) -> None:
    _safe_tree(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _safe_tree(path)
    try:
        with path.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        pass
    if path.stat().st_size != len(data) or path.read_bytes() != data:
        raise ValueError("Existing source retention bytes do not match; no overwrite allowed")


def imap_source_receipt(fetched, source) -> SourceCaptureReceipt:
    key = source_key(email_id=fetched.email_id, source_type="imap", account_key=source.account_key,
                     mailbox=source.mailbox, uid_validity=source.uid_validity, source_uid=fetched.uid)
    coverage = fetched.source_coverage
    if coverage is None or fetched.raw_source is None:
        return SourceCaptureReceipt(source_key=key, retention="unknown")
    coverage = EmailSourceCoverage.model_validate(coverage.model_dump())
    raw = fetched.raw_source
    if len(raw) > MAX_RAW_SOURCE_BYTES:
        raise ValueError("Raw source exceeds bounded retention size")
    if (coverage.email_id != fetched.email_id or coverage.raw_size_bytes != len(raw)
            or coverage.raw_sha256 != hashlib.sha256(raw).hexdigest()
            or coverage.body_sha256 != hashlib.sha256(fetched.body.encode("utf-8")).hexdigest()):
        raise ValueError("Source inventory does not match captured bytes/body")
    relative = f"{key}/{coverage.raw_sha256}/source.eml"
    return SourceCaptureReceipt(source_key=key, retention="retained", raw_ref=relative, coverage=coverage)


def retain_imap_source(fetched, source) -> SourceCaptureReceipt:
    receipt = imap_source_receipt(fetched, source)
    if receipt.retention == "unknown":
        return receipt
    root = _root()
    _retain_exact(root / receipt.raw_ref, fetched.raw_source)
    encoded = json.dumps(receipt.model_dump(), sort_keys=True, ensure_ascii=True).encode("utf-8")
    _retain_exact((root / receipt.raw_ref).with_name("manifest.json"), encoded)
    return receipt


def fetched_matches_capture(fetched, source, receipt: SourceCaptureReceipt) -> bool:
    """Unknown historical coverage remains unknown; never upgrade a prior receipt."""
    receipt = SourceCaptureReceipt.model_validate(receipt.model_dump())
    planned = imap_source_receipt(fetched, source)
    if receipt.source_key != planned.source_key:
        return False
    if receipt.retention == "unknown":
        return True
    return receipt == planned and verify_retained_source(receipt)


def verify_retained_source(receipt: SourceCaptureReceipt) -> bool:
    """Read-only byte verification. It grants neither processing nor delivery authority."""
    try:
        receipt = SourceCaptureReceipt.model_validate(receipt.model_dump())
    except ValueError:
        return False
    if receipt.retention != "retained" or receipt.coverage is None:
        return False
    expected = f"{receipt.source_key}/{receipt.coverage.raw_sha256}/source.eml"
    if receipt.raw_ref != expected or receipt.coverage.raw_size_bytes > MAX_RAW_SOURCE_BYTES:
        return False
    path = _root() / expected
    try:
        _safe_tree(path)
        if path.stat().st_size != receipt.coverage.raw_size_bytes:
            return False
        return hashlib.sha256(path.read_bytes()).hexdigest() == receipt.coverage.raw_sha256
    except (OSError, ValueError):
        return False
