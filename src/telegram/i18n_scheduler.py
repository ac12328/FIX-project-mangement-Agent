"""i18n — Scheduler job card templates and push card body strings."""

STRINGS: dict[str, str] = {
    # ── Morning Brief ──
    "sched.morning_brief_title": "晨間簡報",
    "sched.brief_suppressed": "\n\n📵 自上次簡報以來有 {count} 則通知被隱藏。",

    # ── SLA Alert ──
    "sched.sla_title": "SLA 警報",
    "sched.sla_body": "⏱️ {description}\n{emoji} 緊急程度：{level}\n📅 到期：{due_date}",

    # ── Follow-up Check ──
    "sched.followup_title": "追蹤：{action}",
    "sched.followup_body": "📋 {description}\n🎯 級別：{level}",
    "sched.followup_overdue": " · ⚠️ 逾期 {days} 天",

    # ── Inactivity Alert buttons ──
    # Button display in i18n_cards.py BUTTON_DISPLAY

    # ── Closing Suggestions ──
    "sched.closing_title": "結案建議",
    "sched.closing_body": "📋 已閒置 7 個以上工作日\n🎯 無未結追蹤事項或待處理更新\n💡 準備好正式結案了嗎？",

    # ── Deferred Re-trigger ──
    "sched.deferred_title": "延後重新觸發",
    "sched.deferred_body": "📋 先前延後的更新已可重新審查\n💡 {reason}",

    # ── Email Poll ──
    "sched.email_ticket_title": "📎 工單更新：{ticket_id}",
    "sched.email_ticket_body": "來自 {sender} 的回覆\n主旨：{subject}",
    "sched.email_batch_title": "電子郵件批次摘要",
    "sched.email_batch_body": "📬 {count} 則額外電子郵件通知：\n{items}",
    "sched.client_reply_title": "客戶回覆",
    "sched.client_reply_from": "✉️ 寄件者：{sender}",
    "sched.client_reply_re": "📧 主旨：{subject}",
    "sched.client_reply_summary": "📝 {summary}",
    "sched.client_reply_suggested": "💡 建議：{action}",

    # ── Support Ticket Aging ──
    "sched.ticket_aging_title": "支援工單老化",
    "sched.ticket_aging_body": "🎫 {ticket_id} — {title}\n⏰ 已開啟 {hours} 小時無回應\n對手方：{counterparty}",

    # ── Wiki Curator ──
    "sched.wiki_updated": "📚 *Wiki 已更新* — 已處理 {count} 個項目",
    "sched.wiki_new_pages": "*新頁面（{count}）：*",
    "sched.wiki_updated_pages": "*更新頁面（{count}）：*",

    # ── Wiki Lint ──
    "sched.wiki_lint_title": "📋 *Wiki Lint 警報*",
    "sched.wiki_lint_unverified": "⚠ {count} 個頁面有未驗證的 TODO/verify 標記：",
    "sched.wiki_lint_dead_links": "🔗 找到 {count} 個失效連結",

    # ── Draft Expiry ──
    "sched.draft_expiry": "🗑 **草稿過期** — 已清除 {count} 份過期草稿：",
    "sched.draft_expiry_more": "  …還有 {count} 份",

    # ── Escalation Check ──
    "sched.escalation_alert": "🔴 **升級警報** — {count} 個專案需要關注：",
    "sched.escalation_item": "• **{client_name}** — {days} 天未活動（里程碑：{milestone}）",
    "sched.escalation_consider": "\n請考慮升級或聯繫相關方。",

    # ── Card Push (card_push.py) ──
    "push.draft_ready_title": "草稿就緒（尚未寄出）",
    "push.draft_ready_body": "📧 收件者：{to_email}\n📋 主旨：{subject}\n🆔 草稿：`{draft_id}`{summary_line}",

    # ── Review Push (review_push.py) ──
    "push.review_title": "審查更新",
    "staged_update.card_title": "暫存更新 — {project_id}",
    "staged_update.card_body": "以下欄位已暫存，請確認後才會寫入 DB：",
    "push.review_risk": "🎯 風險：{emoji} {level}",
    "push.review_reason": "💡 {reason}",
    "push.review_evidence": "📎 證據：{refs}",
    "push.auto_applied_banner": "✅ **已套用（可撤回）**",
    "push.auto_applied_footer": "ℹ️ 「撤銷更新」可還原；「關閉」表示接受此次更新。",
    "push.auto_applied_title": "郵件更新已套用",
    "push.email_review_title": "郵件更新待確認",
    "push.email_info_title": "郵件分析",

    # ── Email analysis card body (email_update_format.py) ──
    "email_card.status_applied": "✅ 狀態：已套用（可撤回）",
    "email_card.status_pending": "⏳ 狀態：待確認（未寫入真相）",
    "email_card.status_info_only": "ℹ️ 狀態：僅分析（無欄位變更）",
    "email_card.status_skipped": "⚠️ 狀態：未套用",
    "email_card.status_generic": "狀態：{status}",
    "email_card.summary_header": "📝 摘要",
    "email_card.fix_header": "🔧 FIX / 連線參數",
    "email_card.fields_applied": "📋 已寫入欄位",
    "email_card.fields_pending": "📋 待確認變更（新值）",
    "email_card.fields_header": "📋 欄位",
    "email_card.confidence": "信心度 {pct}",
    "email_card.footer_applied": "ℹ️ 更新已套用；「撤回這項更新」可要求還原。「略過此通知」不會再次修改專案。",
    "email_card.footer_pending": "ℹ️ 請「批准」寫入，或「拒絕」丟棄。里程碑變更不會自動 silent 寫入。",
    "email_card.title_applied": "郵件更新已套用 · {project_id}",
    "email_card.title_pending": "審核項目更新",
    "email_card.title_info": "郵件分析 · {project_id}",
    "push.update_applied": "✅ **更新已套用**",
    "push.update_applied_body": "📋 專案 `{project_id}` 已成功更新。\n📝 {count} 項欄位變更已寫入專案記錄。",
    "push.update_applied_detail": "  • {field} → {value}",
    "push.update_applied_footer": "ℹ️ 此更新無需再次批准；業務跟進請按以上新記錄處理。",

    # ── Advisory Push (advisory_push.py) ──
    "push.advisory_situation": "📊 {situation}",
    "push.advisory_next_steps": "**下一步：**",
    "push.advisory_escalate": "⚡ 升級 → {team}：{reason}",
    "push.advisory_draft_suggestion": "✉️ 草稿建議：{purpose}",
    "push.advisory_similar": "📁 類似案例：{cases}",
    "push.advisory_confidence": "🎯 信心度：{confidence}%",
    "push.advisory_project_title": "專案顧問",
    "push.advisory_support_title": "支援顧問",
    "push.advisory_ticket_prefix": "🎫 `{ticket_id}` — {title}",

    # ── Inbound Policy (email/inbound_policy.py) ──
    "policy.update_needs_attention": "專案更新需要關注：{subject}",
    "policy.update_body": "寄件人：{sender}\n本次專案分析未成功完成。請先核對原郵件與專案目前記錄，再決定是否重新分析；不要假設之前的步驟全部未執行。",
    "policy.new_email_inquiry": "新電子郵件詢問：{subject}",
    "policy.email_triage": "電子郵件分流：{subject}",
    "policy.email_update_result": "[Email → {project_id}] {result}",

    # ── Error Card (card_push.py) ──
    "push.error_title": "LLM 錯誤",
    "push.error_task": "🤖 任務：{task}",
    "push.error_class": "❌ 錯誤：{error_class}",
    "push.error_trace": "🔗 追蹤：`{trace_id}`",
    "push.error_preview": "📝 {preview}",
    "push.error_hint": "💡 重試以再次嘗試，或忽略以清除。",

    # ── Review Push overflow (review_push.py) ──
    "push.update_more": "  • …還有 {count} 項",

    # ── Scheduler overflow / truncation ──
    "sched.overflow_more": "\n… 還有 {count} 項",
    "sched.wiki_more": "  … 還有 {count} 項",
    "sched.confidence": "\n信心度：{confidence}",

    # ── Contact Learning (triage/contact_learning.py) ──
    "contact.identity_title": "身份檢查（全局聯絡人）",
    "contact.identity_body": (
        "新聯絡人（確認一次，全系統有效）\n"
        "電郵：{email}\n"
        "建議名稱：{name}\n"
        "機構：{org}\n"
        "機構類型：{org_type}\n"
        "最近出現專案：{project_id}\n"
        "解析：{method}\n\n"
        "「確認」→ 加入登錄冊並連結專案（預設機構類型）。\n"
        "「覆寫」→ 強制以 Vendor 登記。\n"
        "「標記」→ 交合規人工處理。"
    ),

    # ── Digest Notifications ──
    "sched.afternoon_digest_title": "午後通知摘要",
    "sched.digest_section_sla": "⏱ SLA 警報",
    "sched.digest_section_followup": "📋 追蹤逾期",
    "sched.digest_section_ticket_aging": "🎫 工單老化",
    "sched.digest_section_email_ambiguous": "📬 待分類郵件",
    "sched.digest_section_email_deferred": "📨 延後郵件",
    "sched.digest_section_support": "🔧 支援工單",
    "sched.digest_empty": "✅ 無待處理通知",
    "sched.digest_overflow": "  …另有 {count} 項",
    "sched.digest_header": "📊 通知摘要 ({time})",
    "sched.digest_pending_header": "📋 待處理通知摘要 ({count})",
    "sched.sla_overdue_days": "已逾期 {days} 天",
    "sched.followup_overdue_paren": " (逾期 {days} 天)",
}
