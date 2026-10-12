"""Source-grounded ticket notifications and conservative low-value suppression."""
from __future__ import annotations

import re
from datetime import date
from typing import Literal
from pydantic import BaseModel
from src.email.body_cleaner import clean_email_body
from src.telegram.i18n import get_ui_language


class TicketEmailNotice(BaseModel):
    kind: Literal["progress", "delivery_failure", "informational"]
    title: str
    body: str
    immediate: bool


def is_delivery_failure(sender: str, subject: str, body: str) -> bool:
    subject_signal = re.search(r"undeliver(?:able|ed)|delivery (?:status notification.*failure|fail(?:ed|ure))|non.delivery|未傳遞|未传递|退信|無法傳遞|无法传递", subject, re.I)
    machine = any(part in sender.lower() for part in ("postmaster@", "mailer-daemon@"))
    body_signal = re.search(r"diagnostic-code:|status:\s*5\.\d|delivery.*failed|could not be delivered", body, re.I)
    return bool(subject_signal or (machine and body_signal))


def is_passive_acknowledgment(body: str, *, has_attachments: bool = False) -> bool:
    """Suppress only a whole, short, unambiguous acknowledgment, never new evidence."""
    if has_attachments:
        return False
    text = clean_email_body(body).strip()
    return bool(re.fullmatch(
        r"(?:thanks(?:\s+you)?|thank you|noted|received|acknowledged|ok|okay|收到|已收到|謝謝|谢谢|知道了)"
        r"(?:[\s,.!，。！]+(?:thanks|thank you|noted|received|謝謝|谢谢))*[\s,.!，。！]*",
        text, re.I,
    ))


def incident_report_request(body: str) -> tuple[str, str | None] | None:
    """Recognize only an explicit newest-author request, never attachment contents."""
    text = clean_email_body(body)
    request = re.search(
        r"(?:please|kindly|could you|can you|請|请)[^\n.!?。！？]{0,180}"
        r"(?:incident report|事故報告|事故报告)[^\n.!?。！？]{0,100}", text, re.I)
    if not request:
        return None
    clause = request.group(0)
    if re.search(r"\b(?:sign|signature)\b|簽署|签署|簽名|签名", clause, re.I):
        action = "sign"
    elif re.search(r"\b(?:confirm|confirmation|acknowledge)\b|確認|确认", clause, re.I):
        action = "confirm"
    else:
        return None
    explicit = re.search(r"(?:\bby|\bbefore|no later than|deadline(?: is|:)?|截至|限期|之前)\s*"
                         r"(20\d{2}-\d{2}-\d{2})(?!\d)", clause, re.I)
    deadline = None
    if explicit:
        try:
            deadline = date.fromisoformat(explicit.group(1)).isoformat()
        except ValueError:
            pass
    return action, deadline


def build_ticket_email_notice(*, ticket_id: str, sender: str, subject: str, body: str,
                             summary: str = "", attachment_names: list[str] | None = None) -> TicketEmailNotice:
    english = get_ui_language() == "en"
    attachments = attachment_names or []
    if is_delivery_failure(sender, subject, body):
        return TicketEmailNotice(kind="delivery_failure", immediate=False,
            title=f"{ticket_id}｜" + ("Email not delivered" if english else "郵件未送達"),
            body=f"{subject[:180]}\n" + (
                "This is a delivery-failure report, not a human reply. Check the failed recipient and delivery reason before resending."
                if english else "這是退信，不是對方回覆。請核對失敗收件人及退信原因，再決定是否補寄。") +
                "\n\n" + ("Report excerpt:\n" if english else "退信原文摘錄：\n") + clean_email_body(body)[:600])
    if is_passive_acknowledgment(body, has_attachments=bool(attachments)):
        return TicketEmailNotice(kind="informational", immediate=False,
            title=f"{ticket_id}｜" + ("Acknowledgment received" if english else "收到確認收件"),
            body="No new request or progress in this acknowledgment." if english else "這封回覆只是確認收到，沒有新要求或進展。")
    request = incident_report_request(body)
    excerpt = clean_email_body(body)[:850]
    lines = [f"{sender}\n{subject[:180]}", "", "Latest email" if english else "最新郵件內容",
             summary[:1300] if summary.strip() else excerpt or ("No readable body; view the original email." if english else "未取得可讀正文，請查看原郵件。")]
    if attachments:
        lines.extend(["", ("Received attachments (receipt does not confirm content was reviewed): " if english else "收到附件（不代表已核對內容）：") + ", ".join(attachments)[:400]])
    if request:
        action, deadline = request
        label = (("Signature requested for incident report" if action == "sign" else "Confirmation requested for incident report")
                 if english else ("事故報告待簽署" if action == "sign" else "事故報告待確認"))
        if deadline:
            label += f" · {deadline}"
        lines.extend(["", "Source request" if english else "原郵件要求", excerpt,
                      "", "Suggested next step" if english else "建議下一步",
                      ("Review the original report, then respond to the sender's signature request."
                       if action == "sign" else "Review the original report, then respond to the sender's confirmation request.")
                      if english else ("核對原始事故報告，再回覆寄件人的簽署要求。" if action == "sign" else "核對原始事故報告，再回覆寄件人的確認要求。")])
        if deadline:
            lines.append(("Sender's explicit deadline: " if english else "寄件人明確要求的期限：") + deadline)
    elif not summary.strip():
        lines.extend(["", "Suggested next step" if english else "建議下一步",
                      "Review the new information and confirm whether it answers the outstanding request." if english else "核對以上新資料是否已回應待辦要求，再決定下一步。"])
    lines.append("Suggestions do not mean a follow-up reminder has been created." if english else "建議不代表已建立跟進提醒。")
    return TicketEmailNotice(kind="progress", immediate=True,
        title=f"{ticket_id}｜" + (label if request else ("New information received" if english else "收到新資料")), body="\n".join(lines))
