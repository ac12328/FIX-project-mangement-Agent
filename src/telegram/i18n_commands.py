"""i18n — Slash command response strings.

User-facing placeholders use [brackets] or backticks — never <angle brackets>,
which Rich/HTML parsers treat as tags and strip.
"""

STRINGS: dict[str, str] = {
    # /start
    "cmd.start": (
        "**AgentFIXBPRO** — FIX 協議接入輔助系統\n\n"
        "常用指令：\n"
        "- `/scan` — 專案組合掃描\n"
        "- `/status [ProjectID]` — 專案狀態\n"
        "- `/pending` — 待處理草稿與審查\n"
        "- `/brief` — 晨間簡報\n"
        "- `/track` — 建立追蹤事項\n"
        "- `/query` — 搜尋專案\n"
        "- `/help` — 顯示所有指令"
    ),

    # /status
    "cmd.status_no_projects": "沒有進行中的專案。",
    "cmd.status_header": "進行中的專案：\n",
    "cmd.status_not_found": "找不到專案 '{project_id}'。\n使用 /status 列出所有進行中的專案。",

    # /pending
    "cmd.pending_none": "沒有待處理項目。",

    # /approve
    "cmd.approve_usage": "用法：`/approve [draft_id]`",
    "cmd.draft_not_found": "找不到草稿 {draft_id}。",
    "cmd.approve_cannot": "無法核准草稿 {draft_id}（狀態：{status}）。",
    "cmd.approve_not_queued": "草稿 {draft_id} 未核准或排入寄送：{error}",
    "cmd.approve_queued": "草稿 {draft_id} 已核准並排入寄送；尚未確認寄出。",
    "cmd.approve_in_progress": "草稿 {draft_id} 寄送處理中；請勿再次寄送。",
    "cmd.approve_sent": "草稿 {draft_id} 已核准並寄出。",
    "cmd.approve_send_uncertain": "草稿 {draft_id} 已核准，但送達結果未確認：{error}\n再次寄送前必須先人工核對寄送紀錄。",
    "cmd.approve_send_failed": "草稿 {draft_id} 已核准但寄送失敗：{error}\n使用 /pending 重試或 /reject 捨棄。",

    # /reject
    "cmd.reject_usage": "用法：`/reject [draft_id]`",
    "cmd.reject_discarded": "草稿 {draft_id} 已捨棄。",
    "cmd.reject_cannot": "無法捨棄草稿 {draft_id}（狀態：{status}）。",

    # /note
    "cmd.note_usage": "用法：`/note [ProjectID] [備註文字]`",
    "cmd.note_project_not_found": "找不到專案 {project_id}。",
    "cmd.note_stored": "已為 {project_id} 儲存備註。\n證據 ID：{evidence_id}\n摘要：{summary}",

    # /track
    "cmd.track_usage": "用法：`/track [ProjectID] [到期日 YYYY-MM-DD] [描述]`",
    "cmd.track_project_not_found": "找不到專案 {project_id}。",
    "cmd.track_created": "追蹤事項已建立：{followup_id}\n專案：{project_id}\n到期日：{due_date}\n描述：{description}",

    # /close
    "cmd.close_usage": "用法：`/close [ProjectID]`",
    "cmd.close_project_not_found": "找不到專案 {project_id}。",

    # /reopen
    "cmd.reopen_usage": "用法：`/reopen [ProjectID] [原因]`",

    # /flag
    "cmd.flag_usage": "用法：`/flag [ProjectID] [urgent|watch|clear]`",
    "cmd.flag_invalid": "標記必須是：urgent、watch 或 clear",
    "cmd.flag_cleared": "已清除 {project_id} 的標記。",
    "cmd.flag_set": "{project_id} 已標記為 {flag_value}。將在下一次晨間簡報中計分。",

    # /activity
    "cmd.activity_usage": "用法：`/activity [ProjectID]`",
    "cmd.activity_none": "找不到 {project_id} 的活動記錄。",
    "cmd.activity_header": "活動記錄：{project_id}\n",

    # /set_preference
    "cmd.set_preference_usage": (
        "用法：`/set_preference [ProjectID|ContactID] [欄位=值]`\n"
        "欄位：tone、language、push_style、custom_notes"
    ),
    "cmd.set_preference_format": "格式：欄位=值（例如 tone=formal）",
    "cmd.set_preference_invalid_field": "無效欄位 '{field}'。有效欄位：{valid_fields}",
    "cmd.set_preference_updated": "偏好已更新：{scope_id} → {field}={value}",

    # /delete_preference
    "cmd.delete_preference_usage": "用法：`/delete_preference [project|counterparty] [ID]`",
    "cmd.delete_preference_invalid_scope": "範圍必須是 'project' 或 'counterparty'。",
    "cmd.delete_preference_deleted": "已刪除 {scope} '{scope_id}' 的偏好。",
    "cmd.delete_preference_not_found": "找不到 {scope} '{scope_id}' 的偏好。",

    # /notification
    "cmd.notification_current": "目前通知模式：**{mode}**\n\n用法：`/notification silent|active|emergency`",
    "cmd.notification_invalid": "無效模式。請使用：silent | active | emergency",
    "cmd.notification_set": "通知模式已設定為 **{new_mode}**。",

    # /thinking
    "cmd.thinking_toggled": (
        "Thinking 視窗已**{mode}**（debug 模式）。\n\n"
        "用法：`/thinking`（切換）| `/thinking on` | `/thinking off`"
    ),
    "cmd.thinking_set_on": "Thinking 視窗已**開啟**。回覆會顯示模型推理（💭 Reasoning）與工具進度氣泡。",
    "cmd.thinking_set_off": "Thinking 視窗已**關閉**。推理內容與工具進度將隱藏。",
    "cmd.thinking_invalid": "用法：`/thinking`（切換 on↔off）或 `/thinking on` | `off`",

    # /lang — agent free-text response language (EN/ZH)
    "cmd.lang_current": (
        "目前回覆語言：**{label}**\n\n"
        "用法：\n"
        "`/lang en` — English\n"
        "`/lang zh` — 繁體中文"
    ),
    "cmd.lang_invalid": (
        "無效語言。請使用：`/lang en` | `/lang zh`\n"
        "（別名：english、chinese、zh-tw）"
    ),
    "cmd.lang_set_en": (
        "Language set to **English**.\n"
        "Agent replies and Telegram UI (commands, digests, cards) will use English."
    ),
    "cmd.lang_set_zh": (
        "語言已設為**繁體中文**。\n"
        "Agent 回覆與 Telegram 介面（指令、摘要、卡片）將使用繁體中文。"
    ),
    "cmd.lang_set_zh_cn": (
        "语言已设为**简体中文**（Agent）。\n"
        "Telegram 介面字串目前仍使用繁體中文。"
    ),

    # /help (full text) — GFM-safe: no <angle> placeholders
    "cmd.help": (
        "## AgentFIXBPRO 指令\n\n"
        "### 專案\n"
        "- `/scan` 或 `/portfolio` — 專案組合掃描\n"
        "- `/status [ProjectID] [問題]` — 專案狀態\n"
        "- `/query [文字]` — 搜尋專案\n"
        "- `/newproject [ID] [客戶名] [email]` — 新專案\n"
        "- `/close [ProjectID]` — 關閉專案\n"
        "- `/reopen [ProjectID] [原因]` — 重新開啟專案\n"
        "- `/flag [ProjectID] [urgent|watch|clear]` — 優先標記\n"
        "- `/activity [ProjectID]` — 近期活動\n"
        "- `/note [ProjectID] [文字]` — 儲存會議/通話備註\n"
        "- `/track [ProjectID] [到期日] [描述]` — 建立追蹤事項\n\n"
        "### 草稿與報告\n"
        "- `/pending` — 待處理草稿與審查\n"
        "- `/approve [draft_id]` — 核准草稿\n"
        "- `/reject [draft_id]` — 捨棄草稿\n"
        "- `/brief` — 晨間簡報\n"
        "- `/report [weekly|monthly]` — 產生報告\n"
        "- `/batch [欄位] [值]` — 批次更新\n\n"
        "### 偏好與通知\n"
        "- `/set_preference [ID] [欄位=值]` — 設定偏好\n"
        "- `/delete_preference [project|counterparty] [ID]` — 刪除偏好\n"
        "- `/notification [silent|active|emergency]` — 通知模式\n"
        "- `/thinking [on|off]` — Thinking debug 視窗\n"
        "- `/lang [en|zh]` — 切換回覆語言 (EN / 繁中)\n\n"
        "### 支援\n"
        "- `/ticket [標題] [category=..] [priority=..]` — 建立工單\n"
        "- `/support [status] [limit]` — 未結工單\n"
        "- `/resolve [ticket_id] [文字]` — 記錄解決方案\n"
        "- `/help` — 此訊息\n\n"
        "> 卡片上的行內按鈕可直接執行操作。\n"
        "> 自由文字訊息將路由至主代理。"
    ),

    # /signal_positive
    "cmd.signal_positive_usage": "用法：`/signal_positive [ProjectID]`",
    "cmd.signal_positive_no_drafts": "找不到 {project_id} 的已寄出草稿。",
    "cmd.signal_positive_recorded": "已為 {project_id} 的聯絡人記錄正向訊號（{label}）。",
    "cmd.signal_positive_no_contact": "找不到 {project_id} 的聯絡人 — 無法記錄正向訊號。",

    # /skip
    "cmd.skip_usage": "用法：`/skip [ProjectID]`",
    "cmd.skip_suppressed": "{project_id} 的晨間簡報項目今日已隱藏。",

    # /reply
    "cmd.reply_usage": "用法：`/reply [ProjectID]`",
    "cmd.reply_generated": "已為 {project_id} 產生回覆草稿。使用 /pending 查看。",
    "cmd.reply_failed": "無法為 {project_id} 產生草稿：{error}",

    # /query
    "cmd.query_usage": "用法：`/query [搜尋文字]`",

    # /newproject
    "cmd.newproject_usage": "用法：`/newproject [ProjectID] [客戶名稱] [client_email]`",
    "cmd.newproject_card_id": "📋 ID：`{project_id}`",
    "cmd.newproject_card_client": "👤 客戶：{client_name}",
    "cmd.newproject_card_email": "📧 電子郵件：{email}",
    "cmd.newproject_card_type": "🔌 類型：{connection_type}",
    "cmd.batch_no_projects": "沒有需要更新的專案。",
    "cmd.batch_usage": (
        "用法：`/batch [欄位] [值] [filter=值] [confirm]`\n"
        "範例：`/batch ball_with Internal market=HK confirm`"
    ),
    "cmd.batch_complete": "批次完成：{applied} 項已套用，{failed} 項失敗/拒絕。",

    # /ticket
    "cmd.ticket_usage": (
        "用法：`/ticket [標題] [category=fix_session|network|cert|trade|post_trade|config|access|other]` "
        "`[priority=critical|high|medium|low]`"
    ),
    "cmd.ticket_title_required": "錯誤：工單標題為必填。",

    # /resolve
    "cmd.resolve_usage": "用法：`/resolve [ticket_id] [解決方案描述]`",

    # support_chat_handlers.py strings (surfaced via /ticket, /support, /resolve)
    "support.create_title_required": "錯誤：標題為必填。",
    "support.ticket_created": "工單已建立：{ticket_id}\n  標題：{title}\n  分類：{category} | 嚴重性：{severity}\n  狀態：open",
    "support.ticket_already_exists": "工單已存在：{ticket_id}（{title}）。請直接更新現有工單。",
    "support.update_missing_fields": "錯誤：ticket_id 和欄位為必填。",
    "support.update_invalid_field": "錯誤：欄位 '{field}' 不可更新。有效欄位：{valid_fields}",
    "support.ticket_not_found": "找不到工單 {ticket_id}。",
    "support.ticket_updated": "工單 {ticket_id} 已更新：{field} = {value}",
    "support.ticket_update_failed": "無法更新工單 {ticket_id}。",
    "support.no_tickets": "找不到工單。",
    "support.tickets_found": "找到 {count} 張工單：",
    "support.desk_header": "🎫 Desk issues — 要我哋處理 {needs} · 等緊 {waiting} · 過期 {stale}",
    "support.desk_stale_hint": "💡 {stale} 張過期工單可用 /support stale 或 Dashboard 關閉",
    "support.advisory_ticket_not_found": "找不到工單 {ticket_id}。",
    "support.advisory_project_not_found": "找不到專案 {project_id}。",
    "support.advisory_missing_id": "錯誤：請提供 ticket_id 或 project_id。",
    "support.resolution_text_required": "錯誤：resolution_text 為必填。",
    "support.resolution_no_pattern": "解決方案已記錄，但無法從描述中提取可重用的模式。",
    "support.resolution_stored": "解決方案已儲存為模式 `{pattern_id}`（分類：{category}，信心度：{confidence}）。",
    "support.resolution_ticket_resolved": "\n工單 {ticket_id} 已標記為已解決。",
    "support.advisory_header": "{context_label} 的建議（信心度：{confidence}）：",
    "support.advisory_next_steps": "建議下一步：",
    "support.advisory_escalation": "升級：{team} — {reason}",
    "support.advisory_similar_cases": "類似過往案例：{cases}",
}
