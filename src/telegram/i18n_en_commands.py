"""English strings — slash commands and support chat replies."""

STRINGS_EN: dict[str, str] = {
    "cmd.start": (
        "**AgentFIXBPRO** — FIX Protocol onboarding copilot\n\n"
        "Common commands:\n"
        "- `/scan` — Portfolio scan\n"
        "- `/status [ProjectID]` — Project status\n"
        "- `/pending` — Pending drafts / reviews\n"
        "- `/brief` — Morning brief\n"
        "- `/track` — Create follow-up\n"
        "- `/query` — Search projects\n"
        "- `/help` — Show all commands"
    ),
    "cmd.status_no_projects": "No active projects.",
    "cmd.status_header": "Active projects:\n",
    "cmd.status_not_found": (
        "Project '{project_id}' not found.\nUse /status to list active projects."
    ),
    "cmd.pending_none": "Nothing pending.",
    "cmd.approve_usage": "Usage: `/approve [draft_id]`",
    "cmd.draft_not_found": "Draft {draft_id} not found.",
    "cmd.approve_cannot": "Cannot approve draft {draft_id} (status: {status}).",
    "cmd.approve_not_queued": "Draft {draft_id} was not approved or queued: {error}",
    "cmd.approve_queued": "Draft {draft_id} approved and queued; send not yet confirmed.",
    "cmd.approve_in_progress": "Draft {draft_id} delivery is in progress; do not retry.",
    "cmd.approve_sent": "Draft {draft_id} approved and sent.",
    "cmd.approve_send_uncertain": (
        "Draft {draft_id} was approved, but delivery is unconfirmed: {error}\n"
        "Manual reconciliation is required before another send."
    ),
    "cmd.approve_send_failed": (
        "Draft {draft_id} approved but send failed: {error}\n"
        "Use /pending to retry or /reject to discard."
    ),
    "cmd.reject_usage": "Usage: `/reject [draft_id]`",
    "cmd.reject_discarded": "Draft {draft_id} discarded.",
    "cmd.reject_cannot": "Cannot discard draft {draft_id} (status: {status}).",
    "cmd.note_usage": "Usage: `/note [ProjectID] [note text]`",
    "cmd.note_project_not_found": "Project {project_id} not found.",
    "cmd.note_stored": (
        "Note stored for {project_id}.\nEvidence ID: {evidence_id}\nSummary: {summary}"
    ),
    "cmd.track_usage": "Usage: `/track [ProjectID] [due date YYYY-MM-DD] [description]`",
    "cmd.track_project_not_found": "Project {project_id} not found.",
    "cmd.track_created": (
        "Follow-up created: {followup_id}\nProject: {project_id}\n"
        "Due: {due_date}\nDescription: {description}"
    ),
    "cmd.close_usage": "Usage: `/close [ProjectID]`",
    "cmd.close_project_not_found": "Project {project_id} not found.",
    "cmd.reopen_usage": "Usage: `/reopen [ProjectID] [reason]`",
    "cmd.flag_usage": "Usage: `/flag [ProjectID] [urgent|watch|clear]`",
    "cmd.flag_invalid": "Flag must be: urgent, watch, or clear",
    "cmd.flag_cleared": "Cleared flag on {project_id}.",
    "cmd.flag_set": "{project_id} flagged as {flag_value}. Will score in next morning brief.",
    "cmd.activity_usage": "Usage: `/activity [ProjectID]`",
    "cmd.activity_none": "No activity for {project_id}.",
    "cmd.activity_header": "Activity: {project_id}\n",
    "cmd.set_preference_usage": (
        "Usage: `/set_preference [ProjectID|ContactID] [field=value]`\n"
        "Fields: tone, language, push_style, custom_notes"
    ),
    "cmd.set_preference_format": "Format: field=value (e.g. tone=formal)",
    "cmd.set_preference_invalid_field": "Invalid field '{field}'. Valid: {valid_fields}",
    "cmd.set_preference_updated": "Preference updated: {scope_id} → {field}={value}",
    "cmd.delete_preference_usage": "Usage: `/delete_preference [project|counterparty] [ID]`",
    "cmd.delete_preference_invalid_scope": "Scope must be 'project' or 'counterparty'.",
    "cmd.delete_preference_deleted": "Deleted preference for {scope} '{scope_id}'.",
    "cmd.delete_preference_not_found": "No preference found for {scope} '{scope_id}'.",
    "cmd.notification_current": (
        "Notification mode: **{mode}**\n\nUsage: `/notification silent|active|emergency`"
    ),
    "cmd.notification_invalid": "Invalid mode. Use: silent | active | emergency",
    "cmd.notification_set": "Notification mode set to **{new_mode}**.",
    "cmd.thinking_toggled": (
        "Thinking window **{mode}** (debug mode).\n\n"
        "Usage: `/thinking` (toggle) | `/thinking on` | `/thinking off`"
    ),
    "cmd.thinking_set_on": (
        "Thinking window **on**. Replies will show model reasoning (💭) and tool progress."
    ),
    "cmd.thinking_set_off": "Thinking window **off**. Reasoning and tool progress hidden.",
    "cmd.thinking_invalid": "Usage: `/thinking` (toggle) or `/thinking on` | `off`",
    "cmd.lang_current": (
        "Current language: **{label}**\n\n"
        "Usage:\n"
        "`/lang en` — English\n"
        "`/lang zh` — Traditional Chinese"
    ),
    "cmd.lang_invalid": (
        "Invalid language. Use: `/lang en` | `/lang zh`\n"
        "(aliases: english, chinese, zh-tw)"
    ),
    "cmd.lang_set_en": (
        "Language set to **English**.\n"
        "Agent replies and Telegram UI (commands, digests, cards) will use English."
    ),
    "cmd.lang_set_zh": (
        "Language set to **Traditional Chinese**.\n"
        "Agent replies and Telegram UI will use Traditional Chinese."
    ),
    "cmd.lang_set_zh_cn": (
        "Language set to **Simplified Chinese** (agent).\n"
        "Telegram UI pack currently uses Traditional Chinese strings."
    ),
    "cmd.help": (
        "## AgentFIXBPRO commands\n\n"
        "### Projects\n"
        "- `/scan` or `/portfolio` — Portfolio scan\n"
        "- `/status [ProjectID] [question]` — Project status\n"
        "- `/query [text]` — Search projects\n"
        "- `/newproject [ID] [client] [email]` — New project\n"
        "- `/close [ProjectID]` — Close project\n"
        "- `/reopen [ProjectID] [reason]` — Reopen project\n"
        "- `/flag [ProjectID] [urgent|watch|clear]` — Priority flag\n"
        "- `/activity [ProjectID]` — Recent activity\n"
        "- `/note [ProjectID] [text]` — Store meeting/call note\n"
        "- `/track [ProjectID] [due] [description]` — Create follow-up\n\n"
        "### Drafts & reports\n"
        "- `/pending` — Pending drafts & reviews\n"
        "- `/approve [draft_id]` — Approve draft\n"
        "- `/reject [draft_id]` — Discard draft\n"
        "- `/brief` — Morning brief\n"
        "- `/report [weekly|monthly]` — Generate report\n"
        "- `/batch [field] [value]` — Batch update\n\n"
        "### Preferences & notifications\n"
        "- `/set_preference [ID] [field=value]` — Set preference\n"
        "- `/delete_preference [project|counterparty] [ID]` — Delete preference\n"
        "- `/notification [silent|active|emergency]` — Notification mode\n"
        "- `/thinking [on|off]` — Thinking debug window\n"
        "- `/lang [en|zh]` — Switch language (EN / 繁中)\n\n"
        "### Support\n"
        "- `/ticket [title] [category=..] [priority=..]` — Create ticket\n"
        "- `/support [status] [limit]` — Open tickets\n"
        "- `/resolve [ticket_id] [text]` — Record resolution\n"
        "- `/help` — This message\n\n"
        "> Inline card buttons perform actions directly.\n"
        "> Free-text messages route to the lead agent."
    ),
    "cmd.signal_positive_usage": "Usage: `/signal_positive [ProjectID]`",
    "cmd.signal_positive_no_drafts": "No sent drafts found for {project_id}.",
    "cmd.signal_positive_recorded": (
        "Recorded positive signal for {project_id} contact ({label})."
    ),
    "cmd.signal_positive_no_contact": (
        "No contact for {project_id} — cannot record positive signal."
    ),
    "cmd.skip_usage": "Usage: `/skip [ProjectID]`",
    "cmd.skip_suppressed": "Morning brief item for {project_id} hidden for today.",
    "cmd.reply_usage": "Usage: `/reply [ProjectID]`",
    "cmd.reply_generated": "Reply draft generated for {project_id}. Use /pending to review.",
    "cmd.reply_failed": "Could not generate draft for {project_id}: {error}",
    "cmd.query_usage": "Usage: `/query [search text]`",
    "cmd.newproject_usage": "Usage: `/newproject [ProjectID] [client name] [client_email]`",
    "cmd.newproject_card_id": "📋 ID: `{project_id}`",
    "cmd.newproject_card_client": "👤 Client: {client_name}",
    "cmd.newproject_card_email": "📧 Email: {email}",
    "cmd.newproject_card_type": "🔌 Type: {connection_type}",
    "cmd.batch_no_projects": "No projects to update.",
    "cmd.batch_usage": (
        "Usage: `/batch [field] [value] [filter=value] [confirm]`\n"
        "Example: `/batch ball_with Internal market=HK confirm`"
    ),
    "cmd.batch_complete": "Batch complete: {applied} applied, {failed} failed/rejected.",
    "cmd.ticket_usage": (
        "Usage: `/ticket [title] "
        "[category=fix_session|network|cert|trade|post_trade|config|access|other]` "
        "`[priority=critical|high|medium|low]`"
    ),
    "cmd.ticket_title_required": "Error: ticket title is required.",
    "cmd.resolve_usage": "Usage: `/resolve [ticket_id] [resolution description]`",
    "support.create_title_required": "Error: title is required.",
    "support.ticket_created": (
        "Ticket created: {ticket_id}\n  Title: {title}\n"
        "  Category: {category} | Severity: {severity}\n  Status: open"
    ),
    "support.ticket_already_exists": (
        "Ticket already exists: {ticket_id} ({title}). Update the existing ticket."
    ),
    "support.update_missing_fields": "Error: ticket_id and field are required.",
    "support.update_invalid_field": (
        "Error: field '{field}' is not updatable. Valid: {valid_fields}"
    ),
    "support.ticket_not_found": "Ticket {ticket_id} not found.",
    "support.ticket_updated": "Ticket {ticket_id} updated: {field} = {value}",
    "support.ticket_update_failed": "Could not update ticket {ticket_id}.",
    "support.no_tickets": "No tickets found.",
    "support.tickets_found": "Found {count} ticket(s):",
    "support.desk_header": "🎫 Desk issues — Needs us {needs} · Waiting {waiting} · Stale {stale}",
    "support.desk_stale_hint": "💡 {stale} stale ticket(s) — use /support stale or close them on the dashboard",
    "support.advisory_ticket_not_found": "Ticket {ticket_id} not found.",
    "support.advisory_project_not_found": "Project {project_id} not found.",
    "support.advisory_missing_id": "Error: provide ticket_id or project_id.",
    "support.resolution_text_required": "Error: resolution_text is required.",
    "support.resolution_no_pattern": (
        "Resolution recorded, but no reusable pattern could be extracted."
    ),
    "support.resolution_stored": (
        "Resolution stored as pattern `{pattern_id}` "
        "(category: {category}, confidence: {confidence})."
    ),
    "support.resolution_ticket_resolved": "\nTicket {ticket_id} marked resolved.",
    "support.advisory_header": "Advice for {context_label} (confidence: {confidence}):",
    "support.advisory_next_steps": "Suggested next steps:",
    "support.advisory_escalation": "Escalation: {team} — {reason}",
    "support.advisory_similar_cases": "Similar past cases: {cases}",
}
