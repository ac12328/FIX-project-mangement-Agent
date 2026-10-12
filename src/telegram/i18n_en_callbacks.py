"""English strings — callback / card action replies."""

STRINGS_EN: dict[str, str] = {
    "review.no_project": "No project specified for review.",
    "review.approved_no_changes": "No update applied: this {project_id} review is already handled or no longer valid. Check current pending reviews.",
    "review.approved_applied": "Approved and applied {project_id}: {applied}",
    "review.approval_processed_but": "Approval processed, but: {reason_text}",
    "review.rejected": "Rejected review for {project_id}.",
    "review.edit_mode": "Editing {project_id} — send your changes as text.",
    "review.generic_action": "Review action: {action}",
    "triage.send_project_id": "Send the project ID to assign this email to.",
    "triage.email_skipped": "Email skipped.",
    "triage.email_escalated": "Email escalated — flagged for human review.",
    "triage.no_summary": "(summary unavailable)",
    "triage.generic_action": "Triage action: {action}",
    "draft.no_pending": "No pending draft found.",
    "draft.sent": "Draft {draft_id} sent.",
    "draft.save_style_prompt": "\n\nSave this style as a preference for future drafts?",
    "draft.approved_send_failed": "Draft {draft_id} approved but send failed: {error}",
    "draft.edit_mode": "Draft edit mode — send your revised text.",
    "draft.no_pending_discard": "No pending draft to discard.",
    "draft.discarded": "Draft {draft_id} discarded.",
    "draft.generic_action": "Draft action: {action}",
    "draft.style_pref_not_saved": "Style preference not saved.",
    "draft.style_pref_draft_not_found": "Draft not found — preference not saved.",
    "draft.style_pref_no_contact": "No contact for this project — preference not saved.",
    "draft.style_pref_saved": (
        "Style saved as preference — will affect future draft tone and wording."
    ),
    "draft.style_pref_error": "Could not save preference: {exc}",
    "draft.style_pref_unknown_action": "Unrecognized style preference action.",
    "inbound.no_details": "No details",
    "inbound.latest_reply": "Latest reply for {project_id}:\n{text}",
    "inbound.no_reply_events": "No reply events for {project_id}.",
    "inbound.describe_update": (
        "Describe the update for {project_id} — I will process via the write layer."
    ),
    "inbound.draft_reply_generated": (
        "Reply draft generated for {project_id}. Use /pending to review."
    ),
    "inbound.draft_reply_failed": "Could not generate draft for {project_id}: {error}",
    "inbound.dismissed": "Reply dismissed.",
    "inbound.undo_applied": "✅ Undid auto-update: {revert_text}",
    "inbound.undo_not_available": "No auto-update available to undo.",
    "inbound.generic_action": "Inbound reply action: {action}",
    "inbound.proposal_expired": "This proposal expired or was already handled.",
    "inbound.no_proposal_data": "Proposal data not found.",
    "inbound.proposal_updated": "Updated {project_id}: {parts}",
    "inbound.proposal_no_change": "{project_id} needs no change (already up to date).",
    "inbound.proposal_rejected": "Update for {project_id} rejected: {reason}",
    "inbound.describe_update_agent": (
        "Describe the update for {project_id} — I will handle via the agent."
    ),
    "identity.sender_confirmed_added": "Sender {sender_email} confirmed and added to registry.",
    "identity.suggestion_not_applied": "Contact suggestion {suggestion_id} could not be applied.",
    "identity.contact_confirmed": "Contact {contact_id} confirmed and verified.",
    "identity.send_merge_id": "Send the contact ID to merge into.",
    "identity.contact_ignored": "Contact {contact_id} ignored.",
    "identity.sender_overridden": "Sender {sender_email} confirmed via operator override.",
    "identity.override_failed": "Override failed for {contact_id}.",
    "identity.flagged": "Contact flagged for review. Ref: {reference}",
    "identity.generic_action": "Identity action: {action}",
    "ambiguous.email_assigned": "Email assigned to project {project_id}.",
    "ambiguous.no_project": "No project selected.",
    "new_project.intake_discarded": "New project intake discarded.",
    "new_project.generic_action": "New project action: {action}",
    "sla.snoozed_24h": "Go-live deadline alerts for {project_id} paused for 24 hours; the target date is unchanged.",
    "sla.marked_done": "Marked SLA for {project_id} as done.",
    "sla.escalated": "{project_id} flagged for escalation; this action did not send an escalation email.",
    "sla.acknowledged": "Acknowledgment recorded for {project_id}; project state and due dates are unchanged.",
    "sla.draft_status_generated": "Status email draft saved for review. Check Drafts if the Telegram card does not appear.",
    "sla.draft_skipped_pending": "No new draft was created. Check Drafts or try again.",
    "sla.generic_action": "SLA action: {action}",
    "followup.completed": "Follow-up {followup_id} completed.",
    "followup.could_not_complete": "Could not complete.",
    "followup.stale_card": "This follow-up card is stale. Refresh before acting.",
    "followup.snoozed": "Follow-up {followup_id} snoozed to {new_due}.",
    "followup.max_snoozes": (
        "Max snoozes reached — {followup_id} escalated to level 2 for operator review."
    ),
    "followup.could_not_snooze": "Could not snooze {followup_id}.",
    "followup.invalid_owner": "Invalid owner selection.",
    "followup.reassigned": "Follow-up {followup_id} reassigned.",
    "followup.select_owner_body": "📋 {expertise_query}\n\n👤 Choose a contact or tap Other...",
    "followup.send_assignee_name": "Send the new assignee name.",
    "followup.cancelled": "Follow-up {followup_id} cancelled.",
    "followup.could_not_cancel": "Could not cancel.",
    "followup.draft_generated": "Draft saved for review. Check Drafts if the Telegram card does not appear.",
    "followup.draft_skipped_pending": "No new draft was created. Check Drafts or try again.",
    "followup.generic_action": "Follow-up action: {action}",
    "brief.dismissed": "Notification dismissed; project data and follow-up dates are unchanged.",
    "brief.snoozed_7days": "Morning brief snoozed 7 days (until {date}).",
    "brief.acknowledged": "Brief acknowledged.",
    "brief.generic_action": "Brief action: {action}",
    "inactivity.dismissed": (
        "Inactivity alert dismissed. Will remind next business day if still idle."
    ),
    "inactivity.acknowledged": "Inactivity alert acknowledged.",
    "closing.no_project": "No project specified.",
    "closing.postponed": (
        "Closing checklist postponed {days} day(s). Will reappear after {date}."
    ),
    "closing.generic_action": "Closing checklist action: {action}",
    "error.re_executing": "Re-executing {task_type}...",
    "error.retry_logged": "Retry logged — re-trigger the failed action.",
    "error.background_retry": (
        "This is a background job ({task_type}); it will retry on the next schedule."
    ),
    "error.llm_dismissed": "LLM error alert dismissed.",
    "error.generic_action": "LLM error action: {action}",
    "advisory.draft_ready": "Draft ready.",
    "advisory.draft_generated": "✅ Draft generated for {project_id}:\n\n{preview}",
    "advisory.draft_generation_failed": "❌ Draft generation failed for {project_id}.",
    "advisory.reply_draft_ready_for": "✅ Reply draft ready for {ticket_id}:\n\n{preview}",
    "advisory.reply_draft_failed": "❌ Reply draft failed for {ticket_id}.",
    "advisory.unknown_project_action": "Unknown advisor action: {action}",
    "advisory.unknown_support_action": "Unknown support advisor action: {action}",
    "advisory.i_handle_ack": "Got it — I'll check back in {minutes} minute(s) on how it went.",
    "advisory.resolution_followup_support": (
        "🔧 You said you'd handle ticket `{ref}` yourself.\n"
        "Briefly describe how you resolved it? (I'll store the pattern for next time.)\n"
        "_Reply skip to skip recording._"
    ),
    "advisory.resolution_followup_project": (
        "📋 You said you'd handle `{ref}` yourself.\n"
        "Briefly describe what you did? (I'll store the pattern for next time.)\n"
        "_Reply skip to skip recording._"
    ),
    "advisory.resolution_skipped": "Understood — not recording a pattern this time.",
    "advisory.resolution_no_pattern": (
        "Noted — no reusable pattern extracted, but your handling was recorded."
    ),
    "advisory.drafting_email": "⏳ Drafting email for this project — card will update when ready.",
    "advisory.ask_more_ack": "Digging deeper — I'll send a follow-up with more detail.",
    "advisory.stored_reply_draft": (
        "📝 *Reply draft — {ticket_id}*\n\n{preview}\n\n"
        "_Open the dashboard to edit and send._"
    ),
    "advisory.generating_reply": (
        "⏳ Generating reply draft for {ticket_id} — card will update when ready."
    ),
    "advisory.no_ticket_for_draft": "No ticket ID available to generate a draft.",
    "advisory.escalated_to": "Escalated to {escalation_team}.",
    "advisory.choose_category": "Choose the correct category for {ticket_id}:",
    "advisory.reclass_missing": "Missing ticket or category info for reclassification.",
    "advisory.reclassified": "✅ Ticket {ticket_id} reclassified as *{label}*.",
    "batch.missing_session": "Batch: missing session ID.",
    "batch.session_not_found": "Batch session not found or expired.",
    "batch.confirm_all_result": "Batch complete: {applied} applied, {failed} skipped/failed.",
    "batch.rejected_all": "Batch rejected — no changes applied.",
    "batch.editing_item": (
        "Editing {project_id}: send new value for `{field_name}`\n"
        "(or send 'cancel' to go back)"
    ),
    "batch.item_not_editable": "Item not editable (already applied or invalid index).",
    "batch.unknown_action": "Unrecognized batch action: {action}",
    "batch.card_status_pending": "{count} pending",
    "batch.card_status_completed": ", {count} completed",
    "batch.card_more": "  … and {count} more",
    "query.no_session_context": "This query clarification card has no valid session context.",
    "query.session_expired": "This query clarification session expired. Run /query again.",
    "query.send_revised": "Send revised query text and I will re-evaluate.",
    "query.cancelled": "Aggregate query cancelled.",
    "query.unknown_action": "Unknown query clarification action: {action}",
    "attach.email_not_found": "Email not found.",
    "attach.no_open_tickets": "No open tickets for this project. Create one first.",
    "attach.more_tickets": "\n...and {count} more. Use /status for ticket IDs.",
    "attach.select_ticket": "Select the ticket for this email: {note}",
    "attach.email_attached": "✅ Email attached to {ticket_id}",
    "attach.unknown_action": "Unknown attach action.",
    "session.done": "Done.",
    "session.query_cancelled": "Aggregate query cancelled.",
    "session.no_project_context": "This session has no project context. Restart from the card.",
    "session.processed_result": "Processed {project_id}: {result}",
    "session.processed_staged": "Processed {project_id}: update staged pending confirm",
    "session.draft_updated": "Draft {draft_id} updated. Use /pending to review.",
    "session.draft_update_failed": "Could not update draft {draft_id}.",
    "session.no_draft_found": "No draft found for this session.",
    "session.project_not_found": "Project {project_id} not found. Send a valid project ID.",
    "session.email_assigned": "Email assigned to project {project_id}.",
    "session.merge_recorded": "Merge request recorded: {text}",
    "session.followup_reassigned": "Follow-up reassigned to {display_name}.",
    "session.no_contact_found": "No internal contact matching '{query}'. Try again.",
    "session.multiple_matches": "Multiple matches: {names}. Be more specific.",
    "session.resolution_noted": "Thanks, noted.",
    "session.edit_cancelled": "Edit cancelled.",
    "session.batch_edit_invalid": "Invalid batch edit state.",
    "callback.retrying": "Retrying your last request...",
    "callback.retry_no_response": "Retry produced no response.",
    "dispatcher.action_cancelled": "Action cancelled.",
}
