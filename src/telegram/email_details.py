"""Read the exact notification email with factual, read-only coverage labels."""
from __future__ import annotations

import json
import logging

from src.email.image_result_receipts import ImageResultReceipt
from src.email.source_capture import SourceCaptureReceipt, source_key, verify_retained_source
from src.email.source_coverage import coverage_disclosure
from src.telegram._cb_helpers import extract_link, extract_message_metadata
from src.telegram.operator_messages import en

logger = logging.getLogger(__name__)


def _retained_output_count(saved: dict, capture: SourceCaptureReceipt, project_id: str | None) -> int:
    """Count validated local output identities, never review or completion authority."""
    results = saved.get("image_results", {})
    if not isinstance(results, dict) or capture.coverage is None:
        return 0
    selected = {leaf.part_id: leaf.payload_sha256 for leaf in capture.coverage.leaves
                if leaf.image_candidate and leaf.selection == "selected"}
    paths = set()
    count = 0
    for part_id, item in results.items():
        if not isinstance(item, dict) or item.get("authority") != "output_retention_only":
            continue
        try:
            receipt = ImageResultReceipt.model_validate(item.get("result"))
        except ValueError:
            continue
        if (receipt.source_key != capture.source_key or receipt.raw_sha256 != capture.coverage.raw_sha256
                or receipt.email_id != capture.coverage.email_id or receipt.project_id != project_id
                or receipt.part_id != part_id or selected.get(part_id) != receipt.payload_sha256
                or not receipt.description.strip() or receipt.path in paths):
            continue
        paths.add(receipt.path)
        count += 1
    return count


def email_notification_details(conn, context: dict | None, project_id: str | None) -> dict:
    meta = extract_message_metadata(context)
    link = extract_link(context, "email")
    email_id = (link or {}).get("object_ref") or meta.get("email_id")
    row = conn.execute(
        "SELECT subject,sender_email,body_snippet,project_id,received_at FROM processed_emails WHERE email_id=?",
        (email_id,),
    ).fetchone() if email_id else None
    english = en()
    summary = meta.get("email_summary") or ""
    if row:
        from config.defaults import EMAIL_BODY_STORE_CHARS
        body = str(row["body_snippet"] or "")
        lines = [f"{'From' if english else '寄件人'}：{row['sender_email']}",
                 f"{'Subject' if english else '主旨'}：{row['subject']}"]
        work = conn.execute("SELECT * FROM inbound_email_work WHERE email_id=?", (email_id,)).fetchone()
        coverage_known = False
        if work:
            try:
                saved = json.loads(work["checkpoint_json"])
                if not isinstance(saved, dict):
                    raise ValueError("Invalid source checkpoint")
                capture = SourceCaptureReceipt.model_validate(saved["source_capture"])
                key = source_key(email_id=email_id, source_type=work["source_type"],
                                 account_key=work["account_key"], mailbox=work["mailbox"],
                                 uid_validity=work["uid_validity"], source_uid=work["source_uid"])
                if capture.coverage is not None and capture.source_key == key and capture.coverage.email_id == email_id:
                    coverage_known = True
                    lines.append(coverage_disclosure(capture.coverage, retained=verify_retained_source(capture), english=english))
                    count = _retained_output_count(saved, capture, row["project_id"])
                    lines.append((f"Retained per-image outputs: {count}. "
                                  "Retained outputs are not full batch approval or completion attestation.")
                                 if english else (f"已保存逐張圖片結果：{count}。"
                                                  "結果保存並非整批批准或完成確認。"))
            except (KeyError, TypeError, ValueError):
                logger.debug("Email source coverage unavailable", exc_info=True)
        if not coverage_known:
            lines.append("Original body and attachment coverage: unknown (legacy or unavailable record)."
                         if english else "原始正文及附件覆蓋範圍：未知（舊紀錄或紀錄不可用）。")
        if not coverage_known and len(body) >= EMAIL_BODY_STORE_CHARS:
            lines.append("Body may be truncated. Check the original email for complete contents."
                         if english else "正文可能已截斷，完整內容請核對原郵件。")
        lines.append("")
        if body:
            lines.extend(["Saved email body (attachment contents are not included):" if english
                          else "已保存的郵件正文（不含附件內容）：", body])
        elif summary:
            lines.extend(["Saved summary, not the full original email:" if english
                          else "已保存摘要，並非完整原郵件：", summary])
        else:
            lines.append("No readable email body was saved; attachment coverage is separate." if english
                         else "未保存可讀的郵件正文；附件覆蓋範圍另行列出。")
        return {"text": "\n".join(lines), "project_id": row["project_id"], "object_ref": email_id}
    if summary:
        label = "Saved summary, not the full original email:" if english else "已保存摘要，並非完整原郵件："
        return {"text": label + "\n" + summary, "project_id": project_id}
    return {"text": ("No readable email contents were saved for this notification. Open the related ticket or original mailbox."
                     if english else "這則通知未保存可讀的郵件內容。請查看相關工單或原始郵箱。"), "project_id": project_id}
