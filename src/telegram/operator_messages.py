"""User-facing Telegram wording. These helpers never perform business actions."""
from __future__ import annotations

from html.parser import HTMLParser
from urllib.parse import quote

from src.telegram.i18n import get_ui_language


def en() -> bool:
    return get_ui_language() == "en"


def button_label(profile: str, action: str, fallback: str) -> str:
    common = {
        "DISMISS": ("略過此通知", "Dismiss this notification"),
        "APPROVE": ("批准這項更新", "Approve this update"),
        "REJECT": ("拒絕這項更新", "Reject this update"),
        "UNDO": ("撤回這項更新", "Undo this update"),
        "CONFIRM_SEND": ("確認寄出此郵件", "Confirm sending this email"),
        "SEND": ("確認寄出此郵件", "Confirm sending this email"),
        "RETRY": ("查看恢復方式", "View recovery options"),
    }
    scoped = {
        ("Q", "CONFIRM"): ("確認建立跟進", "Confirm creating follow-up"),
        ("F", "CONFIRM"): ("確認建立專案", "Confirm creating project"),
        ("CU", "CONFIRM"): ("批准這項更新", "Approve this update"),
        ("G", "DONE"): ("標記此跟進完成", "Mark this follow-up done"),
        ("G", "CANCEL"): ("取消此跟進", "Cancel this follow-up"),
        ("G", "SNOOZE"): ("將此跟進改為明日到期", "Set this follow-up due tomorrow"),
        ("M", "SNOOZE_1DAY"): ("暫停此專案上線期限提醒1天", "Pause project go-live alert 1 day"),
        ("M", "ACK"): ("確認已查看此提醒", "Acknowledge this alert"),
        ("M", "ESCALATE"): ("標記需升級處理", "Flag for escalation"),
        ("M", "MARK_DONE"): ("標記此事項完成", "Mark this item done"),
        ("I", "RE-EVALUATE"): ("重新產生晨報", "Refresh morning brief"),
        ("I", "SNOOZE_7DAYS"): ("暫停晨報7天", "Pause morning brief 7 days"),
        ("TK", "DETAILS"): ("查看郵件詳情", "View email details"),
        ("B", "DETAILS"): ("查看郵件詳情", "View email details"),
        ("B", "ASSIGN"): ("選擇所屬專案", "Choose project"),
        ("E", "CONFIRM"): ("確認加入聯絡人", "Confirm adding contact"),
        ("E", "OVERRIDE"): ("改登記為供應商", "Register as vendor"),
        ("E", "FLAG"): ("交人工審查身份", "Request identity review"),
    }
    if profile in {"C", "D", "CP"}:
        scoped.update({(profile, "DETAILS"): ("查看完整草稿", "View full draft"),
                       (profile, "DISCARD"): ("捨棄此草稿", "Discard this draft")})
    pair = scoped.get((profile, action)) or common.get(action)
    return pair[1 if en() else 0] if pair else fallback


def dashboard_url(card) -> str | None:
    from config.settings import CONFIG
    base = getattr(CONFIG, "dashboard_base_url", "").rstrip("/")
    if not base.startswith(("http://", "https://")):
        return None
    target_project = card.project_id
    if card.profile == "G":
        # G callback entities are follow-up IDs, not project IDs. Never invent
        # a /projects/{followup_id} URL when the actual parent is unavailable.
        target_project = card.metadata.get("project_id")
        if not target_project:
            return None
    pid = quote(target_project or "", safe="")
    ticket = card.metadata.get("ticket_id")
    if ticket:
        path = f"/support/{quote(str(ticket), safe='')}"
    elif card.profile == "A":
        path = f"/reviews?q={pid}" if pid else "/reviews"
    elif card.profile in {"C", "D", "CP"}:
        path = f"/drafts?q={pid}" if pid else "/drafts"
    elif card.profile == "P":
        path = f"/support/{pid}" if pid and (card.project_id or "").startswith("ST-") else "/support"
    elif pid and not (card.project_id or "").startswith("_"):
        path = f"/projects/{pid}" + ("/closing" if card.profile == "J" else "")
    else:
        return None
    return base + path


class _PlainHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.href: str | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "br":
            self.parts.append("\n")
        if tag == "a":
            self.href = dict(attrs).get("href")

    def handle_endtag(self, tag):
        if tag == "a" and self.href:
            self.parts.append(f" ({self.href})")
            self.href = None

    def handle_data(self, data):
        self.parts.append(data)


def readable_plain(markdown: str) -> str:
    """Use the same safe Markdown parser as HTML delivery, removing only markup."""
    from src.telegram.stream_format import markdown_to_telegram_html
    parser = _PlainHTML()
    parser.feed(markdown_to_telegram_html(markdown))
    return "".join(parser.parts)


def review_failure(reason: str) -> str:
    low = (reason or "").lower()
    if "superseded" in low or "newer email" in low or "stale" in low:
        return ("No update applied: newer evidence or project changes made this review outdated. Reassess the latest email before approving."
                if en() else "未套用更新：已有較新的郵件或專案變更，這張卡的依據已過時。請重新評估最新郵件後再審核。")
    if "permission" in low or "authorized" in low or "four" in low:
        return ("No update applied: authorization requirements were not met. Ask an authorized reviewer to check this item."
                if en() else "未套用更新：未符合授權要求，請由有權限的審核者檢查這項更新。")
    return ("Update not confirmed as applied. Review the decision details in the dashboard before retrying."
            if en() else "未確認更新已套用。請先在Dashboard核對這項審核的處理結果，再決定是否重試。")


def applied_receipt(project_id: str, fields: dict, *, current_next_action: str | None = None, advisory_refresh: bool = False) -> str:
    from src.telegram.email_update_format import field_label
    lines = [f"{project_id}｜" + ("Update applied" if en() else "更新已套用")]
    for key, value in fields.items():
        shown = str(getattr(value, "value", value)) if value not in (None, "") else ("Cleared" if en() else "已清除")
        lines.append(f"• {field_label(key)}：{shown}")
    lines.append("Only the fields listed above were updated." if en() else "本次只更新以上欄位。")
    if current_next_action:
        lines.append(("Current saved next action: " if en() else "目前已保存的下一步：") + current_next_action)
    if advisory_refresh:
        lines.append("Earlier advice is no longer action authority. New advice, if needed, will be evaluated separately; no reminder was created by this approval."
                     if en() else "舊建議不再作為操作依據；如有需要會另外重新評估建議。本次批准未建立提醒。")
    return "\n".join(lines)


def draft_receipt(draft: dict, *, sent: bool, error: str | None = None, uncertain: bool = False) -> str:
    if sent:
        title = "Email sent" if en() else "郵件已寄出"
    elif uncertain:
        title = "Email delivery unconfirmed" if en() else "郵件送達結果未確認"
    else:
        title = "Email sending did not complete successfully" if en() else "郵件寄送未成功完成"
    lines = [title, f"{'To' if en() else '收件人'}：{draft.get('to_email') or '—'}",
             f"{'Subject' if en() else '主旨'}：{draft.get('subject') or '—'}"]
    if not sent:
        category = str(error or "").lower()
        if "timeout" in category or "timed out" in category:
            lines.append("Reason: SMTP timeout; delivery may be unconfirmed." if en() else "原因：寄送逾時（SMTP timeout）；送達結果可能尚未確認。")
        elif "auth" in category or "credential" in category:
            lines.append("Reason: sending authentication failed; ask the administrator to check sender settings." if en() else "原因：寄送身份驗證失敗，請管理員核對寄件設定。")
        else:
            lines.append("Reason: sending returned an unsuccessful result; inspect the delivery record for details." if en() else "原因：寄送回報未成功，詳細原因請核對寄送記錄。")
        if uncertain:
            lines.append("Manual reconciliation is required before another send."
                         if en() else "再次寄送前必須先人工核對寄送紀錄。")
        else:
            lines.append("Check sent mail and the delivery result before retrying to avoid duplicate delivery."
                         if en() else "請先核對寄件備份及寄送結果，再決定是否重試，以免重複寄送。")
    lines.append(f"{'Reference' if en() else '參考編號'}：{draft.get('draft_id') or '—'}")
    return "\n".join(lines)
