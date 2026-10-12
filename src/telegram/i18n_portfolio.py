"""i18n — Portfolio, status, reports, inactivity, and morning brief formatting strings."""

STRINGS: dict[str, str] = {
    # ── Portfolio Scan (portfolio_scan.py) ──
    "portfolio.header": "📊 **專案組合健康**",
    "portfolio.summary": "🔴 {red}  🟡 {yellow}  🟢 {green}  📋 {total} 個進行中",
    "portfolio.needs_attention": "🔴 **需要關注** ({count})",
    "portfolio.monitor": "🟡 **監察中** ({count})",
    "portfolio.on_track": "🟢 **正常進行** ({count})",
    "portfolio.all_normal": "一切正常。",
    "portfolio.hint": "💡 /status `PROJECT_ID` 查看詳情",

    # ── Status Answer (status/answer.py) ──
    "status.ball": "🎾 Ball：{ball_with}",
    "status.next": "➡️ 下一步：{next_action}",
    "status.blockers": "🚧 障礙：{blockers}",
    "status.go_live": "📅 目標上線：{target}",
    "status.days_since": "📅 距上次變更：{days} 天 {sla_icon}",
    "status.recommended": "💡 建議：{action}",
    "status.escalation": "⚠️ 升級：{reason}",

    # ── Periodic Reports (periodic_reports.py) ──
    "report.header": "📊 **{report_type}報告**",
    "report.period": "📅 {start} → {end}",
    "report.summary": "📋 進行中：{active} · 新增：{new} · 完成：{completed} · 受阻：{blocked}",
    "report.highlights": "**重點摘要**",
    "report.milestone_dist": "**里程碑分佈**",
    "report.projects": "**專案** ({count})",

    # ── Inactivity Alert (inactivity_alert.py) ──
    "inactivity.header": "🚨 **系統提醒**",
    "inactivity.operator_inactive": "⚠️ 操作員已閒置 {days} 個工作日",
    "inactivity.last_seen": "🕐 最後上線：{date}",
    "inactivity.pending_items": "📋 待處理項目：",
    "inactivity.no_pending": "📋 沒有待處理項目。",

    # ── Morning Brief Fallback (morning_brief.py format_brief_text) ──
    "brief.header": "📋 **晨間簡報** · {date}",
    "brief.stats": "📊 進行中：{active} · 草稿：{drafts} · 追蹤：{followups}",
    "brief.needs_attention": "🔴 **需要關注** ({count})",
    "brief.monitor": "🟡 **監察中** ({count})",
    "brief.on_track": "🟢 **正常進行** ({count})",
    "brief.all_normal": "一切正常。",
    "brief.action_header": "📌 **今日行動清單**",
    "brief.action_email_likely": "📧 較可能需要處理的外部郵件",
    "brief.action_email_review": "📧 近日未評分客信",
    "brief.action_draft": "📝 待審批草稿",
    "brief.action_followup": "⏰ 逾期跟進",
    "brief.action_internal": "🔧 逾期內部任務",
    "brief.no_actions": "✅ 今日無待處理事項",
    "brief.desk_header": "🎫 **Desk issues**",
    "brief.desk_counts": "要我哋處理 {needs} · 等緊對方 {waiting} · 過期可關 {stale}",
    "brief.desk_stale_hint": "💡 {stale} 張過期工單可喺 Dashboard /support 一掣關閉",
    "brief.desk_empty": "沒有未完結工單。",
    "brief.portfolio_header": "📋 **項目全景** — 共 {total} 個活躍項目",

    # ── Report Highlights (periodic_reports.py) ──
    "report.highlight_completed": "{count} 個專案已完成",
    "report.highlight_started": "{count} 個新專案已開始",
    "report.highlight_blocked": "{count} 個專案目前受阻",

    # ── Inactivity Alert Items (inactivity_alert.py) ──
    "inactivity.unacknowledged_cards": "{count} 張未確認卡片",
    "inactivity.escalation_l2": "{count} 個專案處於升級等級 2",
    "inactivity.overdue_followups": "{count} 項逾期內部追蹤",

    # ── Status Answer (status/answer.py) ──
    "status.pending_q": "🤔 待處理問題：{question}",

    # ── Morning Brief operator inactive banner ──
    "brief.operator_inactive": "⚠️ 操作員已閒置 {days} 個工作日",

    # ── Portfolio Scan score label ──
    "portfolio.score": "分數",

    # ── Freetext Error Fallbacks (handlers_freetext.py) ──
    "freetext.internal_error": "處理訊息時發生內部錯誤。請重試或使用 /status 或 /query。",
    "freetext.no_reply": "無法回覆此訊息。請重試或使用 /help。",
}
