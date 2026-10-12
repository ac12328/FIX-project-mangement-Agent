"""Fact-versus-hypothesis wording for support advice; no claimed diagnosis."""
from src.email.body_cleaner import clean_email_body
from src.models.advisory import NextStep
from src.telegram.i18n import get_ui_language


def grounded_support_guidance(ticket, source: str = ""):
    english = get_ui_language() == "en"
    text = clean_email_body(source or ticket.description or ticket.title or "")
    reported = text[:650]
    assessment = (("Reported information (source excerpt):\n" if english else "已收到的資料（原文摘錄）：\n") + reported +
        ("\nCause is not confirmed. The following are investigation suggestions, not a diagnosis." if english else "\n原因尚未確認；以下是待驗證的排查建議，不是已確認原因。"))
    if "throttl" in text.lower() or "rate limit" in text.lower() or "限速" in text:
        actions = (["Compare both sides' FIX logs for the rejected and accepted orders in the same time window.",
                    "Confirm how each side counts messages and applies the rate-limit window before changing the limit."] if english else
                   ["比對同一時段雙方FIX log，包括被拒及成功的訂單。", "向雙方確認訊息計數及限速時間窗，再決定是否調整設定。"])
    else:
        actions = (["Review the exact error and relevant logs against the reported issue.",
                    "Confirm missing details and the responsible team before changing configuration."] if english else
                   ["按郵件所報問題，先核對實際錯誤及相關日誌。", "確認欠缺資料及負責團隊後，再決定是否修改設定。"])
        # Preserve useful category knowledge as optional checks, never asserted causes
        # or premature configuration changes. Case-specific signals above take priority.
        from src.advisory.knowledge import get_support_advisory
        category = str(getattr(ticket.category, "value", ticket.category))
        knowledge = get_support_advisory(category) or {}
        checks = knowledge.get("diagnostic_steps", [])[:3]
        if checks:
            actions = actions[:1] + [
                ("Optional check, pending evidence: " if english else "待驗證的檢查選項：") + item for item in checks
            ]
    if ticket.counterparty:
        assessment = f"{ticket.counterparty}\n{assessment}"
    steps = [NextStep(action=a, reason="Investigation suggestion; root cause unconfirmed", priority="now" if i == 0 else "soon") for i,a in enumerate(actions)]
    return assessment, steps
