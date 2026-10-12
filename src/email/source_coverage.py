"""Inventory returned MIME bytes without treating selected processing as full coverage.

No network, model, domain writes, or classification decisions belong here.
"""
from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class MimeLeafCoverage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    part_id: str
    declared_type: str
    detected_image_type: str | None = None
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    attachment_candidate: bool
    image_candidate: bool
    selection: Literal["body", "selected", "metadata_only", "not_selected", "unsupported"]


class EmailSourceCoverage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1] = 1
    email_id: str = Field(min_length=1)
    raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_size_bytes: int = Field(ge=0)
    body_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    body_chars: int = Field(ge=0)
    stored_body_chars: int = Field(ge=0)
    parser_defects: int = Field(ge=0)
    text_decode_failures: int = Field(ge=0)
    leaves: tuple[MimeLeafCoverage, ...]

    @model_validator(mode="after")
    def consistent(self):
        if self.stored_body_chars > self.body_chars:
            raise ValueError("Stored body cannot exceed original body")
        if len({leaf.part_id for leaf in self.leaves}) != len(self.leaves):
            raise ValueError("MIME part identities must be unique")
        return self

    @property
    def body_truncated(self) -> bool:
        return self.stored_body_chars < self.body_chars

    @property
    def original_image_count(self) -> int:
        return sum(leaf.image_candidate for leaf in self.leaves)

    @property
    def selected_image_count(self) -> int:
        return sum(leaf.image_candidate and leaf.selection == "selected" for leaf in self.leaves)

    @property
    def image_selection_complete(self) -> bool:
        return (not self.parser_defects and not self.text_decode_failures
                and all(not leaf.image_candidate or leaf.selection == "selected"
                        for leaf in self.leaves))


def detect_image_type(payload: bytes) -> str | None:
    """Report byte signatures, not image validity, semantics, or safe skip authority."""
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if payload.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if payload[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if payload.startswith(b"RIFF") and payload[8:12] == b"WEBP":
        return "image/webp"
    if payload.startswith(b"BM"):
        return "image/bmp"
    if payload[:4] in (b"II\x2a\x00", b"MM\x00\x2a"):
        return "image/tiff"
    return None


def inventory_source(raw: bytes, msg, *, email_id: str, body: str,
                     attachments: list, stored_body_limit: int) -> EmailSourceCoverage:
    """Inventory all MIME leaves, including those omitted by existing extraction caps."""
    selected = {a.mime_part_id: a for a in attachments if a.mime_part_id}
    leaves = []
    defects = decode_failures = 0
    for index, part in enumerate(msg.walk()):
        if part.is_multipart():
            defects += len(part.defects)
            continue
        payload = part.get_payload(decode=True) or b""
        defects += len(part.defects)
        declared = part.get_content_type().lower()
        detected = detect_image_type(payload)
        is_image = declared.startswith("image/") or detected is not None
        candidate = (is_image or bool(part.get_filename())
                     or part.get_content_disposition() in {"attachment", "inline"})
        part_id = f"mime-{index}"
        attachment = selected.get(part_id)
        if not candidate:
            selection = "body"
        elif attachment is not None:
            selection = "selected" if attachment.data else "metadata_only"
        else:
            selection = "not_selected"
        # A generic-binary image has been retained, not processed by the image pipeline.
        if is_image and not declared.startswith("image/"):
            selection = "unsupported"
        if declared in {"text/plain", "text/html"}:
            try:
                payload.decode(part.get_content_charset() or "utf-8", errors="strict")
            except (UnicodeError, LookupError):
                decode_failures += 1
        leaves.append(MimeLeafCoverage(
            part_id=part_id, declared_type=declared, detected_image_type=detected,
            payload_sha256=hashlib.sha256(payload).hexdigest(), size_bytes=len(payload),
            attachment_candidate=candidate, image_candidate=is_image, selection=selection,
        ))
    return EmailSourceCoverage(
        email_id=email_id, raw_sha256=hashlib.sha256(raw).hexdigest(), raw_size_bytes=len(raw),
        body_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(), body_chars=len(body),
        stored_body_chars=min(len(body), max(0, stored_body_limit)), parser_defects=defects,
        text_decode_failures=decode_failures, leaves=tuple(leaves),
    )


def coverage_disclosure(coverage: EmailSourceCoverage, *, retained: bool, english: bool = True) -> str:
    """User-facing factual counts; no assertion of semantic review or completeness."""
    binary = sum(leaf.declared_type == "application/octet-stream" for leaf in coverage.leaves)
    if not english:
        return (f"原始來源位元組：{'已保存' if retained else '未確認已保存'}。"
                f"已存正文：{coverage.stored_body_chars}/{coverage.body_chars} 字元"
                f"{'（已截斷）' if coverage.body_truncated else ''}。"
                f"已選圖片：{coverage.selected_image_count}/{coverage.original_image_count}。"
                f"通用二進位附件：{binary}。保存或選取並不代表附件內容已完整審閱。")
    return (f"Original source bytes: {'retained' if retained else 'not verified as retained'}. "
            f"Saved body: {coverage.stored_body_chars}/{coverage.body_chars} characters"
            f"{' (truncated)' if coverage.body_truncated else ''}. "
            f"Selected images: {coverage.selected_image_count}/{coverage.original_image_count}. "
            f"Generic-binary parts: {binary}. "
            "Selection and retention do not certify attachment review.")
