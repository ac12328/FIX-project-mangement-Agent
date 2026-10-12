"""Surface milestone lag as a reviewable historical assessment, not a write."""
from __future__ import annotations

from src.project_reconciliation.progress_review import preview_lagging_project


def route_lag_review(conn, result, *, language: str, post_commit=None):
    from src.email.project_truth.serve import (
        EmailProjectTruthServeApplicationResult, LagReviewCommandReceipt,
    )
    caller_transaction = conn.in_transaction
    preview = preview_lagging_project(
        conn, project_id=result.project_id, source_ref=result.source_ref,
        post_commit=post_commit,
    )
    english = language == "en"
    if preview is not None and preview.proposed_update_id and preview.status == "needs_review":
        return EmailProjectTruthServeApplicationResult(
            status="completed", write_action="milestone_review_required",
            summary=(
                "Historical email evidence supports a milestone catch-up review; no project fields were changed."
                if english else "已根據歷史電郵建立進度補正審核；專案欄位尚未變更。"
            ),
            suggested_action=(
                f"Review {preview.proposed_update_id} in Dashboard Pending reviews. Verify the evidence and any unresolved identity or support issues before approval."
                if english else f"請到 Dashboard 的 Pending reviews 處理 {preview.proposed_update_id}；批准前請核對證據、專案名稱及未解決問題。"
            ),
            proposed_fields=tuple(sorted(preview.proposed_fields.items())),
            should_push_info_card=True, confidence=0.0,
            state_write_action="milestone_review_required",
            state_proposed_fields=tuple(sorted(preview.proposed_fields.items())),
            lag_review_command=LagReviewCommandReceipt(
                project_id=result.project_id, source_ref=result.source_ref,
                preview_result=preview.status,
                status=("persisted_in_caller_transaction" if caller_transaction else "committed"),
                proposed_update_id=preview.proposed_update_id,
                proposed_fields=tuple(sorted(preview.proposed_fields.items())),
            ),
        )
    failed = preview is None or not preview.ok
    return EmailProjectTruthServeApplicationResult(
        status="failed" if failed else "completed",
        write_action="application_failed" if failed else "skipped",
        summary=(
            "Milestone lag remains unresolved; no project fields were changed."
            if english else "里程碑落後問題仍未解決；專案欄位未變更。"
        ),
        suggested_action=(
            "The historical review could not be created. Retry the assessment; do not treat this as a state update."
            if failed and english else "The evidence did not establish a qualifying transition. Obtain source-bound confirmation and reassess."
            if english else "未能建立歷史進度審核，請重試；這次並未更新專案。"
            if failed else "現有證據未滿足里程碑契約，請補充具來源的確認後重新評估。"
        ),
        should_push_info_card=True, confidence=0.0,
        lag_review_command=LagReviewCommandReceipt(
            project_id=result.project_id, source_ref=result.source_ref,
            preview_result=preview.status if preview is not None else "unavailable",
            status="incomplete" if failed else "not_applied",
            proposed_fields=(tuple(sorted(preview.proposed_fields.items()))
                             if preview is not None else ()),
        ),
    )
