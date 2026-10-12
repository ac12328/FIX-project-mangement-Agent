"""Impact-first failure notices with verified, not promised, recovery timing."""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from src.telegram.operator_messages import en

BACKGROUND_MAINTENANCE = frozenset({"wiki_curator", "knowledge_mining", "wiki_lint", "wiki_weekly_health"})
_NAMES = {
    "email_poll": ("收信處理", "Email processing"),
    "wiki_curator": ("知識庫整理", "Knowledge maintenance"),
    "knowledge_mining": ("郵件知識整理", "Email knowledge extraction"),
    "morning_brief": ("晨報產生", "Morning brief"),
    "afternoon_digest": ("通知摘要", "Notification digest"),
    "followup_check": ("跟進提醒檢查", "Follow-up reminder check"),
    "sla_check": ("期限提醒檢查", "Deadline reminder check"),
}


def error_notice(task: str, error_class: str = "", *, next_run_at=None, reference: str | None = None) -> str:
    english = en()
    name = _NAMES.get(task, ("所要求的處理", "Requested operation"))[1 if english else 0]
    reason = ("Timed out" if english else "處理逾時") if "time" in error_class.lower() else ("Processing error" if english else "處理錯誤")
    lines = [f"{name}｜" + ("Not completed successfully" if english else "未成功完成"),
             f"{'Reason' if english else '原因'}：{reason}"]
    if task in {"email_poll", "sla_check", "followup_check"}:
        lines.append("Impact: the latest messages or reminders may not have been fully processed; check pending items."
                     if english else "影響：最新郵件或提醒可能尚未完整處理，請核對待處理項目。")
    else:
        lines.append("This run did not complete successfully. This notice does not confirm whether any earlier steps succeeded."
                     if english else "這次處理未成功完成；此通知不代表之前的步驟全部未執行，請勿直接假設可以重做。")
    verified_next = isinstance(next_run_at, datetime) and next_run_at.tzinfo and next_run_at > datetime.now(timezone.utc)
    if verified_next:
        stamp = next_run_at.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M HKT")
        lines.append(("Next scheduled run: " if english else "已排定下次執行：") + stamp)
    else:
        lines.append("No retry has been started by this notice; a next run time has not been verified. Check the processing record before rerunning."
                     if english else "此通知沒有啟動重試，亦未確認下次執行時間。請先核對處理記錄，再決定是否重做。")
    if reference:
        lines.append(("Support reference: " if english else "查詢參考：") + reference[:90])
    return "\n".join(lines)


def next_scheduled_run(context, task: str):
    """Read a matching live job only; never turn an absent schedule into a promise."""
    try:
        queue = getattr(context, "job_queue", None)
        if queue is None:
            queue = getattr(getattr(context, "application", None), "job_queue", None)
        jobs = queue.get_jobs_by_name(task) if queue is not None else ()
        times = [job.next_t for job in jobs if isinstance(getattr(job, "next_t", None), datetime)]
        return min(times) if times else None
    except Exception:
        return None
