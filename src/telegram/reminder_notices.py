"""Useful reminders from persisted responsibility and due-date fields."""
from src.telegram.operator_messages import en


def followup_reminder(conn, followup: dict, *, days_overdue: int = 0) -> str:
    english = en()
    description = followup.get("description") or ("Task details missing; inspect the original request." if english else "缺少具體任務內容，請核對原始要求。")
    owner = followup.get("owner_contact_id")
    if owner:
        row = conn.execute("SELECT display_name FROM contacts WHERE contact_id=?", (owner,)).fetchone()
        owner = row[0] if row else owner
    owner = owner or followup.get("owner_side") or ("Not confirmed" if english else "尚未確認")
    due = followup.get("due_date") or ("Not set" if english else "未設定")
    return "\n".join([
        str(description), f"{'Owner' if english else '負責對象'}：{owner}",
        f"{'Original due date' if english else '原定日期'}：{due}",
        (f"Overdue by {days_overdue} day(s)." if english else f"已逾期{days_overdue}天。") if days_overdue > 0 else ("Please check progress." if english else "請確認進度。"),
        "Confirm whether the request has been fulfilled; if not, follow up or choose a new due date. No reminder was rescheduled by this notice."
        if english else "請確認要求是否已完成；未完成可跟進或選擇新期限。此通知沒有自行延後期限。",
    ])
