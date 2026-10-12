"""Lease-bound per-image receipts, separate from complete batch/domain evidence.

Recorded output is not completion attestation, permission to retry, or proof of
absence of other external effects. This module never invokes a model.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.email.source_capture import SourceCaptureReceipt, source_key, verify_retained_source


class ImageResultReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1] = 1
    source_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    email_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    part_id: str = Field(min_length=1)
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    prompt_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    path: str = Field(min_length=1, max_length=2048)
    description: str = Field(min_length=1, max_length=16000)


class ImageBatchContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_key: str
    raw_sha256: str | None
    email_id: str
    project_id: str
    original_selection_complete: bool | None
    expected: tuple[tuple[str, str], ...]


def _leased_row(conn, email_id, token):
    row = conn.execute("""SELECT * FROM inbound_email_work
        WHERE email_id=? AND route_state='routing' AND lease_token=?
          AND lease_expires_at > datetime('now')""", (email_id, token)).fetchone()
    if row is None:
        raise ValueError("Image work lost its live route lease")
    return row


def batch_context(conn, fetched, project_id: str, token: str) -> ImageBatchContext:
    row = _leased_row(conn, fetched.email_id, token)
    saved = json.loads(row["checkpoint_json"])
    if saved.get("route_steps", {}).get("image_evidence") != "started":
        raise ValueError("Image batch must be started before result retention")
    classified = json.loads(row["classification_json"])
    if classified.get("project_id") != project_id:
        raise ValueError("Image batch project does not match the captured classification")
    key = source_key(email_id=fetched.email_id, source_type=row["source_type"],
                     account_key=row["account_key"], mailbox=row["mailbox"],
                     uid_validity=row["uid_validity"], source_uid=row["source_uid"])
    raw_hash = None
    selection_complete = None  # Legacy: selected-batch only, original coverage unknown.
    capture = None
    if "source_capture" in saved:
        capture = SourceCaptureReceipt.model_validate(saved["source_capture"])
        if capture.source_key != key:
            raise ValueError("Original source identity changed")
        if capture.retention == "retained":
            coverage = fetched.source_coverage
            if (coverage is None or capture.coverage is None
                    or capture.coverage != coverage or coverage.email_id != fetched.email_id
                    or fetched.raw_source is None
                    or hashlib.sha256(fetched.raw_source).hexdigest() != coverage.raw_sha256
                    or hashlib.sha256(fetched.body.encode("utf-8")).hexdigest() != coverage.body_sha256
                    or not verify_retained_source(capture)):
                raise ValueError("Fresh source differs from retained original or retention is unavailable")
            raw_hash = coverage.raw_sha256
            selection_complete = coverage.image_selection_complete
    expected = []
    for index, item in enumerate(fetched.attachments):
        if not item.content_type.startswith("image/"):
            continue
        part_id = item.mime_part_id or f"selected-{index}"
        digest = hashlib.sha256(item.data).hexdigest()
        if not item.data or item.size_bytes != len(item.data) or (
            item.payload_sha256 is not None and item.payload_sha256 != digest
        ):
            raise ValueError("Selected image has missing or changed payload")
        if capture and capture.retention == "retained":
            leaf = next((leaf for leaf in capture.coverage.leaves if leaf.part_id == part_id), None)
            if (leaf is None or leaf.selection != "selected" or not leaf.image_candidate
                    or leaf.payload_sha256 != digest or leaf.size_bytes != len(item.data)):
                raise ValueError("Selected image is not bound to the original MIME payload")
        expected.append((part_id, digest))
    if len({part_id for part_id, _ in expected}) != len(expected):
        raise ValueError("Selected MIME identities must be unique")
    context = ImageBatchContext(source_key=key, raw_sha256=raw_hash, email_id=fetched.email_id,
                                project_id=project_id, expected=tuple(expected),
                                original_selection_complete=selection_complete)
    if "image_batch" in saved or "image_results" in saved:
        raise ValueError("Existing image batch must not be repeated")
    saved["image_batch"] = context.model_dump(mode="json")
    count = conn.execute("""UPDATE inbound_email_work SET checkpoint_json=?, updated_at=datetime('now')
        WHERE email_id=? AND route_state='routing' AND lease_token=?
          AND lease_expires_at > datetime('now') AND checkpoint_json=?""",
        (json.dumps(saved, sort_keys=True), fetched.email_id, token, row["checkpoint_json"])).rowcount
    if count != 1:
        raise ValueError("Image batch lost to concurrent work")
    return context


def validated_result(context: ImageBatchContext, result: dict) -> ImageResultReceipt:
    receipt = ImageResultReceipt(
        source_key=context.source_key, raw_sha256=context.raw_sha256,
        email_id=context.email_id, project_id=context.project_id,
        part_id=result["part_id"], payload_sha256=result["payload_sha256"],
        prompt_sha256=result.get("prompt_sha256"), path=result["path"],
        description=result["description"],
    )
    if not receipt.description.strip() or (receipt.part_id, receipt.payload_sha256) not in context.expected:
        raise ValueError("Image result is empty or not bound to the expected source payload")
    return receipt


def record_image_result(conn, token: str, context: ImageBatchContext, result: dict) -> None:
    receipt = validated_result(context, result)
    row = _leased_row(conn, context.email_id, token)
    saved = json.loads(row["checkpoint_json"])
    if saved.get("route_steps", {}).get("image_evidence") != "started":
        raise ValueError("Image result cannot be added outside a started batch")
    if saved.get("image_batch") != context.model_dump(mode="json"):
        raise ValueError("Image result does not match the committed batch identity")
    actual_key = source_key(email_id=context.email_id, source_type=row["source_type"],
                            account_key=row["account_key"], mailbox=row["mailbox"],
                            uid_validity=row["uid_validity"], source_uid=row["source_uid"])
    if actual_key != context.source_key or json.loads(row["classification_json"]).get("project_id") != context.project_id:
        raise ValueError("Image result source or project changed")
    capture = saved.get("source_capture", {})
    if context.raw_sha256 != (capture.get("coverage") or {}).get("raw_sha256"):
        raise ValueError("Image result original source hash changed")
    results = saved.get("image_results", {})
    if not isinstance(results, dict):
        raise ValueError("Invalid per-image receipt store")
    encoded = receipt.model_dump()
    prior = results.get(receipt.part_id)
    if prior is not None:
        if prior.get("result") != encoded:
            raise ValueError("Existing image output must never be overwritten")
        return
    if any(item["result"]["path"] == receipt.path for item in results.values()):
        raise ValueError("Duplicate image result paths cannot certify distinct payloads")
    saved["image_results"] = {**results, receipt.part_id: {
        "result": encoded, "recorded_at": datetime.now(timezone.utc).isoformat(),
        "authority": "output_retention_only",
    }}
    count = conn.execute("""UPDATE inbound_email_work SET checkpoint_json=?, updated_at=datetime('now')
        WHERE email_id=? AND route_state='routing' AND lease_token=?
          AND lease_expires_at > datetime('now') AND checkpoint_json=?""",
        (json.dumps(saved, sort_keys=True), context.email_id, token, row["checkpoint_json"])).rowcount
    if count != 1:
        raise ValueError("Image result checkpoint lost to concurrent work")


def validate_batch_completion(conn, token: str, context: ImageBatchContext,
                              receipts: list[ImageResultReceipt]) -> None:
    """Read guards inside the final evidence transaction; output retention is not approval."""
    row = _leased_row(conn, context.email_id, token)
    saved = json.loads(row["checkpoint_json"])
    if (saved.get("image_batch") != context.model_dump(mode="json")
            or saved.get("route_steps", {}).get("image_evidence") != "started"
            or json.loads(row["classification_json"]).get("project_id") != context.project_id
            or context.original_selection_complete is False):
        raise ValueError("Image batch identity/coverage changed before evidence commit")
    key = source_key(email_id=context.email_id, source_type=row["source_type"],
                     account_key=row["account_key"], mailbox=row["mailbox"],
                     uid_validity=row["uid_validity"], source_uid=row["source_uid"])
    if key != context.source_key:
        raise ValueError("Image batch source identity changed before evidence commit")
    if context.raw_sha256 is not None:
        capture = SourceCaptureReceipt.model_validate(saved["source_capture"])
        if capture.source_key != context.source_key or capture.coverage.raw_sha256 != context.raw_sha256 or not verify_retained_source(capture):
            raise ValueError("Original source retention changed before evidence commit")
    results = saved.get("image_results", {})
    if set(results) != {part_id for part_id, _ in context.expected}:
        raise ValueError("Per-image receipt set is incomplete")
    expected_results = {receipt.part_id: receipt.model_dump() for receipt in receipts}
    if len(expected_results) != len(receipts) or set(expected_results) != set(results):
        raise ValueError("Returned image receipts differ from the retained output set")
    for part_id, digest in context.expected:
        if (results[part_id].get("authority") != "output_retention_only"
                or results[part_id].get("result") != expected_results[part_id]):
            raise ValueError("Retained image output changed before evidence commit")
        receipt = ImageResultReceipt.model_validate(results[part_id]["result"])
        if (receipt.source_key != context.source_key or receipt.raw_sha256 != context.raw_sha256
                or receipt.email_id != context.email_id or receipt.project_id != context.project_id
                or receipt.part_id != part_id or receipt.payload_sha256 != digest
                or not receipt.description.strip()):
            raise ValueError("Per-image output receipt identity changed before evidence commit")
