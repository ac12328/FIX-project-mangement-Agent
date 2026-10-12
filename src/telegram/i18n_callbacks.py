"""i18n — Callback handler response strings."""

STRINGS: dict[str, str] = {
    # ── Profile A: Review ──
    "review.no_project": "未指定要審查的專案。",
    "review.approved_no_changes": "未套用任何更新：{project_id} 這項審核已處理或不再有效，請查看目前待審核項目。",
    "review.approved_applied": "已核准並套用 {project_id}：{applied}",
    "review.approval_processed_but": "核准已處理，但：{reason_text}",
    "review.rejected": "已拒絕 {project_id} 的審查。",
    "review.edit_mode": "正在編輯 {project_id} — 請以文字傳送您的修改。",
    "review.generic_action": "審查操作：{action}",

    # ── Profile B: Triage ──
    "triage.send_project_id": "請傳送要指派此電子郵件的專案 ID。",
    "triage.email_skipped": "電子郵件已略過。",
    "triage.email_escalated": "電子郵件已升級 — 已標記為人工審查。",
    "triage.no_summary": "（摘要不可用）",
    "triage.generic_action": "分流操作：{action}",

    # ── Profile C/D: Draft ──
    "draft.no_pending": "找不到待處理的草稿。",
    "draft.sent": "草稿 {draft_id} 已寄出。",
    "draft.save_style_prompt": "\n\n要將此風格儲存為未來草稿的偏好設定嗎？",
    "draft.approved_send_failed": "草稿 {draft_id} 已核准但寄送失敗：{error}",
    "draft.edit_mode": "草稿編輯模式 — 請傳送您修改後的文字。",
    "draft.no_pending_discard": "沒有待處理的草稿可以捨棄。",
    "draft.discarded": "草稿 {draft_id} 已捨棄。",
    "draft.generic_action": "草稿操作：{action}",
    "draft.style_pref_not_saved": "未儲存風格偏好。",
    "draft.style_pref_draft_not_found": "找不到草稿 — 未儲存偏好。",
    "draft.style_pref_no_contact": "此專案找不到聯絡人 — 未儲存偏好。",
    "draft.style_pref_saved": "風格已儲存為偏好 — 將影響未來草稿的語氣和措辭。",
    "draft.style_pref_error": "無法儲存偏好：{exc}",
    "draft.style_pref_unknown_action": "無法識別的風格偏好操作。",

    # ── Profile L: Inbound Reply ──
    "inbound.no_details": "無詳細資訊",
    "inbound.latest_reply": "{project_id} 的最新回覆：\n{text}",
    "inbound.no_reply_events": "找不到 {project_id} 的回覆事件。",
    "inbound.describe_update": "請描述 {project_id} 的更新 — 我將透過寫入層處理。",
    "inbound.draft_reply_generated": "已為 {project_id} 產生回覆草稿。使用 /pending 查看。",
    "inbound.draft_reply_failed": "無法為 {project_id} 產生草稿：{error}",
    "inbound.dismissed": "回覆已關閉。",
    "inbound.undo_applied": "✅ 已撤銷自動更新：{revert_text}",
    "inbound.undo_not_available": "無可撤銷的自動更新記錄。",
    "inbound.generic_action": "收信回覆操作：{action}",
    "inbound.proposal_expired": "此提案已過期或已處理。",
    "inbound.no_proposal_data": "找不到提案資料。",
    "inbound.proposal_updated": "已更新 {project_id}：{parts}",
    "inbound.proposal_no_change": "{project_id} 無需變更（已是最新狀態）。",
    "inbound.proposal_rejected": "{project_id} 的更新被拒絕：{reason}",
    "inbound.describe_update_agent": "請描述 {project_id} 的更新 — 我將透過代理處理。",

    # ── Profile E: Identity ──
    "identity.sender_confirmed_added": "寄件者 {sender_email} 已確認並加入登錄冊。",
    "identity.suggestion_not_applied": "聯絡人建議 {suggestion_id} 無法套用。",
    "identity.contact_confirmed": "聯絡人 {contact_id} 已確認並驗證。",
    "identity.send_merge_id": "請傳送要合併的聯絡人 ID。",
    "identity.contact_ignored": "聯絡人 {contact_id} 已忽略。",
    "identity.sender_overridden": "寄件者 {sender_email} 已透過操作員覆寫確認。",
    "identity.override_failed": "{contact_id} 的覆寫失敗。",
    "identity.flagged": "聯絡人已標記待審查。參考：{reference}",
    "identity.generic_action": "身份操作：{action}",

    # ── Profile H: Ambiguous Routing ──
    "ambiguous.email_assigned": "電子郵件已指派至專案 {project_id}。",
    "ambiguous.no_project": "未選擇專案。",

    # ── Profile F: New Project Intake ──
    "new_project.intake_discarded": "新專案接收已捨棄。",
    "new_project.generic_action": "新專案操作：{action}",

    # ── Profile M: SLA ──
    "sla.snoozed_24h": "已暫停 {project_id} 的上線期限提醒24小時；原定上線日期未變更。",
    "sla.marked_done": "已將 {project_id} 的 SLA 標記為完成。",
    "sla.escalated": "已將 {project_id} 標記為需要升級處理；此操作未寄出升級郵件。",
    "sla.acknowledged": "已記錄你查看了 {project_id} 的提醒；未修改專案狀態或期限。",
    "sla.draft_status_generated": "狀態電子郵件草稿已儲存，待審核。如未見 Telegram 卡片，請到草稿頁查看。",
    "sla.draft_skipped_pending": "未有建立新草稿。請查看現有草稿，或稍後再試。",
    "sla.generic_action": "SLA 操作：{action}",

    # ── Profile G: Follow-up ──
    "followup.completed": "追蹤事項 {followup_id} 已完成。",
    "followup.could_not_complete": "無法完成。",
    "followup.snoozed": "追蹤事項 {followup_id} 已延後至 {new_due}。",
    "followup.max_snoozes": "已達最大延後次數 — {followup_id} 已升級至第 2 級由操作員審查。",
    "followup.could_not_snooze": "無法延後 {followup_id}。",
    "followup.invalid_owner": "無效的負責人選擇。",
    "followup.reassigned": "追蹤事項 {followup_id} 已重新指派。",
    "followup.select_owner_body": "📋 {expertise_query}\n\n👤 選擇聯絡人或點選「其他」...",
    "followup.send_assignee_name": "請傳送新的負責人名稱。",
    "followup.cancelled": "追蹤事項 {followup_id} 已取消。",
    "followup.could_not_cancel": "無法取消。",
    "followup.draft_generated": "草稿已儲存，待審核。如未見 Telegram 卡片，請到草稿頁查看。",
    "followup.draft_skipped_pending": "未有建立新草稿。請查看現有草稿，或稍後再試。",
    "followup.generic_action": "追蹤事項操作：{action}",

    # ── Profile I: Brief ──
    "brief.dismissed": "已略過此通知；專案資料及跟進期限未更改。",
    "brief.snoozed_7days": "晨間簡報已延後 7 天（直到 {date}）。",
    "brief.acknowledged": "簡報已確認。",
    "brief.generic_action": "簡報操作：{action}",

    # ── Inactivity Alert ──
    "inactivity.dismissed": "閒置警報已關閉。若持續閒置，將於下一個工作日重新提醒。",
    "inactivity.acknowledged": "閒置警報已確認。",

    # ── Profile J: Closing ──
    "closing.no_project": "未指定專案。",
    "closing.postponed": "關閉清單已延後 {days} 天。將在 {date} 之後重新出現。",
    "closing.generic_action": "關閉清單操作：{action}",

    # ── Profile N: LLM Error ──
    "error.re_executing": "正在重新執行 {task_type}...",
    "error.retry_logged": "重試已記錄 — 請重新觸發失敗的操作。",
    "error.background_retry": "此為背景工作 ({task_type})，將於下次排程自動重試。",
    "error.llm_dismissed": "LLM 錯誤警報已關閉。",
    "error.generic_action": "LLM 錯誤操作：{action}",

    # ── Profile O/P: Advisory ──
    "advisory.draft_ready": "草稿已完成。",
    "advisory.draft_generated": "✅ 已為 {project_id} 產生草稿：\n\n{preview}",
    "advisory.draft_generation_failed": "❌ {project_id} 的草稿產生失敗。",
    "advisory.reply_draft_ready_for": "✅ 已為 {ticket_id} 準備回覆草稿：\n\n{preview}",
    "advisory.reply_draft_failed": "❌ {ticket_id} 的回覆草稿產生失敗。",
    "advisory.unknown_project_action": "未知的顧問操作：{action}",
    "advisory.unknown_support_action": "未知的支援顧問操作：{action}",
    "advisory.i_handle_ack": "收到 — 我將在 {minutes} 分鐘後詢問您處理結果。",
    "advisory.resolution_followup_support": (
        "🔧 您之前表示會自行處理工單 `{ref}`。\n"
        "請簡述您點樣解決？（我會記低模式，下次類似情況可以更快建議您。）\n"
        "_回覆「略過」可唔記錄。_"
    ),
    "advisory.resolution_followup_project": (
        "📋 您之前表示會自行處理 `{ref}` 嘅問題。\n"
        "請簡述您做咗咩？（我會記低模式，下次可以更快建議您。）\n"
        "_回覆「略過」可唔記錄。_"
    ),
    "advisory.resolution_skipped": "明白 — 今次唔記錄模式。下次類似情況我會再問您。",
    "advisory.resolution_no_pattern": "收到 — 未能提取可重用模式，但您嘅處理方式已記錄。",
    "advisory.drafting_email": "⏳ 正在為此專案起草電子郵件 — 完成後將更新此卡片。",
    "advisory.ask_more_ack": "讓我深入了解 — 我會傳送包含更多細節的後續消息。",
    "advisory.stored_reply_draft": "📝 *回覆草稿 — {ticket_id}*\n\n{preview}\n\n_開啟儀表板以編輯和傳送。_",
    "advisory.generating_reply": "⏳ 正在為 {ticket_id} 產生回覆草稿 — 準備好後將更新此卡片。",
    "advisory.no_ticket_for_draft": "無可用的工單 ID 來產生草稿。",
    "advisory.escalated_to": "已升級至 {escalation_team}。",
    "advisory.choose_category": "請為 {ticket_id} 選擇正確的分類：",
    "advisory.reclass_missing": "缺少工單或分類資訊以進行重新分類。",
    "advisory.reclassified": "✅ 工單 {ticket_id} 已重新分類為 *{label}*。",

    # ── Batch ──
    "batch.missing_session": "批次處理：缺少工作階段 ID。",
    "batch.session_not_found": "批次工作階段未找到或已過期。",
    "batch.confirm_all_result": "批次完成：{applied} 項已套用，{failed} 項已略過/失敗。",
    "batch.rejected_all": "批次已拒絕 — 未套用任何變更。",
    "batch.editing_item": "正在編輯 {project_id}：請傳送 `{field_name}` 的新值\n（或傳送 'cancel' 返回）",
    "batch.item_not_editable": "項目不可編輯（已套用或索引無效）。",
    "batch.unknown_action": "無法識別的批次操作：{action}",
    "batch.card_status_pending": "{count} 項待處理",
    "batch.card_status_completed": "，{count} 項已完成",
    "batch.card_more": "  … 還有 {count} 項更多",

    # ── Query ──
    "query.no_session_context": "此查詢澄清卡片沒有有效的工作階段上下文。",
    "query.session_expired": "此查詢澄清工作階段已過期。請重新執行 /query。",
    "query.send_revised": "請傳送修改後的查詢文字，我將重新評估。",
    "query.cancelled": "聚合查詢已取消。",
    "query.unknown_action": "未知的查詢澄清操作：{action}",

    # ── Ticket Attach ──
    "attach.email_not_found": "找不到電子郵件。",
    "attach.no_open_tickets": "此專案沒有未結工單。請先建立一個。",
    "attach.more_tickets": "\n...還有 {count} 項。使用 /status 查詢工單 ID。",
    "attach.select_ticket": "請選擇此電子郵件所屬的工單：{note}",
    "attach.email_attached": "✅ 電子郵件已附加至 {ticket_id}",
    "attach.unknown_action": "未知的附加操作。",

    # ── Session Flow ──
    "session.done": "完成。",
    "session.query_cancelled": "聚合查詢已取消。",
    "session.no_project_context": "此工作階段沒有專案上下文。請從卡片重新開始。",
    "session.processed_result": "已處理 {project_id}：{result}",
    "session.processed_staged": "已處理 {project_id}：更新已暫存待確認",
    "session.draft_updated": "草稿 {draft_id} 已更新。使用 /pending 查看。",
    "session.draft_update_failed": "無法更新草稿 {draft_id}。",
    "session.no_draft_found": "此工作階段找不到草稿。",
    "session.project_not_found": "找不到專案 {project_id}。請傳送有效的專案 ID。",
    "session.email_assigned": "電子郵件已指派至專案 {project_id}。",
    "session.merge_recorded": "合併請求已記錄：{text}",
    "session.followup_reassigned": "追蹤事項已重新指派給 {display_name}。",
    "session.no_contact_found": "找不到 '{query}' 的內部聯絡人。請再試一次。",
    "session.multiple_matches": "多個符合項目：{names}。請更具體描述。",
    "session.resolution_noted": "謝謝，已記錄。",
    "session.edit_cancelled": "編輯已取消。",
    "session.batch_edit_invalid": "批次編輯狀態無效。",

    # ── Callbacks (callbacks.py) ──
    "callback.retrying": "正在重新嘗試您的上一個請求...",
    "callback.retry_no_response": "重試未產生任何回應。",

    # ── Dispatcher (card_dispatcher.py) ──
    "dispatcher.action_cancelled": "操作已取消。",
}
