"""
Database schema DDL for AgentFIXBPRO.
All CREATE TABLE statements. Apply via apply_schema(conn).
"""

import logging
import sqlite3


logger = logging.getLogger(__name__)


SCHEMA_DDL = """
-- Schema version tracking
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Projects: canonical project truth
CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    client_name TEXT,
    client_email TEXT,
    market TEXT,
    project_status TEXT NOT NULL DEFAULT 'Active'
        CHECK (project_status IN ('Active','Blocked','Waiting','Completed','Archived','Cancelled')),
    ball_with TEXT NOT NULL DEFAULT 'Unknown'
        CHECK (ball_with IN ('Internal','External','Shared','Unknown')),
    current_milestone TEXT
        CHECK (current_milestone IS NULL OR current_milestone IN (
            'M1_NetworkSetup','M2_UATSessionConfiguration','M3_UATEnvironmentAccess',
            'M4_ApplicationTesting','M5_ConformanceTestCases',
            'M6_TestReportAndProductionPreparation',
            'M7_LogReviewAndProductionSessionDelivery','M8_GoLive'
        )),
    broker_code TEXT,
    connection_type TEXT DEFAULT 'FIX 4.2',
    target_go_live TEXT,
    notes TEXT,
    sla_limit REAL,
    logbook TEXT,
    next_action TEXT,
    operator_id TEXT NOT NULL DEFAULT 'default_operator',
    project_tier TEXT CHECK (project_tier IS NULL OR project_tier IN ('high','medium','low')),
    work_item_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_activity_at TEXT
);

-- Milestone change audit trail
CREATE TABLE IF NOT EXISTS milestone_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    from_milestone TEXT,
    to_milestone TEXT NOT NULL,
    changed_by TEXT NOT NULL DEFAULT 'system',
    changed_at TEXT NOT NULL DEFAULT (datetime('now')),
    reason TEXT,
    evidence_ref TEXT,
    proposed_update_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_milestone_history_project
    ON milestone_history(project_id, changed_at DESC);

-- Project events (inbound/outbound activity spine)
CREATE TABLE IF NOT EXISTS project_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    event_type TEXT NOT NULL,
    event_direction TEXT,
    event_at TEXT NOT NULL,
    source_agent TEXT,
    source_surface TEXT,
    source_kind TEXT,
    email_id TEXT,
    subject TEXT,
    summary TEXT,
    participants_json TEXT,
    metadata_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_project_events_project
    ON project_events(project_id, event_at DESC);

-- Episodes (narrative timeline entries)
CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    ts TEXT NOT NULL,
    date TEXT,
    title TEXT,
    summary TEXT,
    source_email TEXT,
    confidence REAL DEFAULT 1.0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_episodes_project
    ON episodes(project_id, ts DESC);

-- Learned rules (knowledge base)
CREATE TABLE IF NOT EXISTS learned_rules (
    rule_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT,
    trigger_type TEXT NOT NULL DEFAULT 'procedure',
    source_project_id TEXT,
    condition TEXT,
    rule TEXT,
    extra_knowledge TEXT DEFAULT '{}',
    scope TEXT NOT NULL DEFAULT 'global',
    applicable_to TEXT NOT NULL DEFAULT '["*"]',
    status TEXT NOT NULL DEFAULT 'pending_review',
    conflict_with TEXT NOT NULL DEFAULT '[]',
    usage_count INTEGER DEFAULT 0,
    confidence REAL DEFAULT 0.8,
    expanded_by TEXT,
    expanded_at TEXT,
    written_to_kb INTEGER DEFAULT 0,
    kb_file TEXT,
    kb_written_at TEXT
);

-- Evidence store (append-only)
CREATE TABLE IF NOT EXISTS evidence_store (
    evidence_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    evidence_type TEXT NOT NULL
        CHECK (evidence_type IN ('email','commitment','fact','attachment','manual_note')),
    content_summary TEXT NOT NULL,
    content_text TEXT,
    source_ref TEXT,
    evidence_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_evidence_project
    ON evidence_store(project_id, evidence_at DESC);

-- Working memory (persisted, 7-day staleness flag)
CREATE TABLE IF NOT EXISTS working_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    memory_key TEXT NOT NULL,
    memory_value TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(project_id, memory_key)
);

-- Operator state (global, not project-specific) — Blueprint Section 34
CREATE TABLE IF NOT EXISTS operator_state (
    id INTEGER PRIMARY KEY,
    last_telegram_interaction_at TEXT,
    inactivity_alert_dismissed_until TEXT,
    notification_mode TEXT DEFAULT 'active'
        CHECK (notification_mode IN ('silent','active','emergency')),
    session_timeout_minutes INTEGER DEFAULT 10,
    max_daily_notifications INTEGER DEFAULT 15,
    notification_delivery_mode TEXT DEFAULT 'realtime'
        CHECK (notification_delivery_mode IN ('realtime','digest_3x','digest_2x')),
    quiet_hours_start TEXT,
    quiet_hours_end TEXT,
    notifications_sent_today INTEGER DEFAULT 0,
    notifications_reset_date TEXT,
    response_language TEXT DEFAULT 'zh-TW'
        CHECK (response_language IN ('zh-TW','en','zh-CN')),
    thinking_window_enabled INTEGER NOT NULL DEFAULT 0,
    harness_require_wiki_progress INTEGER,
    harness_auto_draft_rule58 INTEGER,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Organisations (Contact Registry v1)
CREATE TABLE IF NOT EXISTS organisations (
    org_id TEXT PRIMARY KEY,
    org_name TEXT NOT NULL,
    domains TEXT DEFAULT '[]',
    org_type TEXT DEFAULT 'unknown'
        CHECK (org_type IN ('client','vendor','regulator','internal','unknown')),
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Contacts (Contact Registry v1 — people are global)
CREATE TABLE IF NOT EXISTS contacts (
    contact_id TEXT PRIMARY KEY,
    display_name TEXT,
    contact_type TEXT DEFAULT 'person'
        CHECK (contact_type IN ('person','department','unknown','correspondent','internal')),
    email_addresses TEXT DEFAULT '[]',
    primary_email TEXT,
    organisation_id TEXT REFERENCES organisations(org_id),
    role TEXT,
    cross_project_history TEXT DEFAULT '[]',
    created_by TEXT DEFAULT 'auto_resolved'
        CHECK (created_by IN ('auto_resolved', 'operator_confirmed')),
    confidence REAL DEFAULT 0.5,
    notes TEXT,
    department TEXT,
    expertise_tags TEXT DEFAULT '[]',
    responsiveness_score REAL,
    preferred_contact_method TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Project ↔ Organisation membership (party graph Option C)
CREATE TABLE IF NOT EXISTS project_parties (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    org_id TEXT NOT NULL REFERENCES organisations(org_id),
    party_role TEXT NOT NULL DEFAULT 'other'
        CHECK (party_role IN (
            'client','vendor','regulator','internal','counterparty','other'
        )),
    is_primary INTEGER NOT NULL DEFAULT 0,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(project_id, org_id, party_role)
);
CREATE INDEX IF NOT EXISTS idx_project_parties_project
    ON project_parties(project_id);
CREATE INDEX IF NOT EXISTS idx_project_parties_org
    ON project_parties(org_id);

-- Person ↔ Project membership
CREATE TABLE IF NOT EXISTS contact_project_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id TEXT NOT NULL REFERENCES contacts(contact_id),
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    role_on_project TEXT DEFAULT 'other'
        CHECK (role_on_project IN (
            'pm','developer','ops','cert','compliance','other'
        )),
    is_primary INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'identity_confirm',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(contact_id, project_id)
);
CREATE INDEX IF NOT EXISTS idx_contact_project_links_project
    ON contact_project_links(project_id);
CREATE INDEX IF NOT EXISTS idx_contact_project_links_contact
    ON contact_project_links(contact_id);

CREATE TABLE IF NOT EXISTS contact_suggestions (
    suggestion_id TEXT PRIMARY KEY,
    email_id TEXT REFERENCES processed_emails(email_id),
    sender_email TEXT NOT NULL,
    sender_name TEXT,
    project_id TEXT REFERENCES projects(project_id),
    organisation_id_guess TEXT REFERENCES organisations(org_id),
    organisation_name_guess TEXT,
    organisation_domain TEXT,
    org_type_guess TEXT DEFAULT 'unknown'
        CHECK (org_type_guess IN ('client','vendor','regulator','internal','unknown')),
    resolution_method TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','applied','ignored')),
    applied_contact_id TEXT REFERENCES contacts(contact_id),
    telegram_message_id TEXT,
    last_pushed_at TEXT,
    operator_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(email_id, sender_email, project_id)
);
CREATE INDEX IF NOT EXISTS idx_contact_suggestions_status
    ON contact_suggestions(status);
CREATE INDEX IF NOT EXISTS idx_contact_suggestions_project
    ON contact_suggestions(project_id);
CREATE INDEX IF NOT EXISTS idx_contact_suggestions_email_status
    ON contact_suggestions(sender_email, status);

-- Response pattern memory
CREATE TABLE IF NOT EXISTS response_pattern_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id TEXT NOT NULL REFERENCES contacts(contact_id),
    project_id TEXT REFERENCES projects(project_id),
    preferred_greeting TEXT,
    tone TEXT DEFAULT 'semi-formal',
    response_length_preference TEXT DEFAULT 'medium',
    preferred_language TEXT DEFAULT 'en',
    avoid_phrases TEXT DEFAULT '[]',
    positive_signals TEXT DEFAULT '[]',
    source TEXT DEFAULT 'default',
    confidence REAL DEFAULT 0.5,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(contact_id, project_id)
);

-- Operator preferences
CREATE TABLE IF NOT EXISTS operator_preferences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL CHECK (scope IN ('project','counterparty')),
    project_id TEXT,
    counterparty_id TEXT,
    tone TEXT DEFAULT 'semi-formal',
    language TEXT DEFAULT 'bilingual',
    push_style TEXT DEFAULT 'reactive',
    custom_notes TEXT,
    operator_id TEXT NOT NULL DEFAULT 'default_operator',
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Drafts
CREATE TABLE IF NOT EXISTS drafts (
    draft_id TEXT PRIMARY KEY,
    project_id TEXT REFERENCES projects(project_id),
    task_type TEXT,
    draft_version INTEGER DEFAULT 1,
    current_draft_text TEXT,
    draft_history TEXT DEFAULT '[]',
    status TEXT DEFAULT 'created'
        CHECK (status IN ('created','in_review','approved','sending','sent','discarded','send_failed')),
    to_email TEXT,
    cc TEXT,
    subject TEXT,
    body_html TEXT,
    body_text TEXT,
    sender_identity_id TEXT,
    recipients_json TEXT NOT NULL DEFAULT '{"to":[],"cc":[],"bcc":[]}',
    delivery_mode TEXT NOT NULL DEFAULT 'unresolved'
        CHECK (delivery_mode IN ('unresolved','new','reply')),
    reply_to_email_id TEXT,
    suggested_reply_to_email_id TEXT,
    owner_user_id TEXT,
    reviewer_user_id TEXT,
    review_state TEXT NOT NULL DEFAULT 'not_requested'
        CHECK (review_state IN ('not_requested','requested','changes_requested','approved','cancelled','stale')),
    review_requested_by TEXT,
    review_requested_at TEXT,
    approval_hash TEXT,
    approved_at TEXT,
    workspace_revision INTEGER NOT NULL DEFAULT 1,
    checkpoint_version INTEGER NOT NULL DEFAULT 0,
    send_affecting_hash TEXT,
    attachment_hash TEXT,
    delivery_status TEXT NOT NULL DEFAULT 'editing'
        CHECK (delivery_status IN ('editing','scheduled','undoable','sending','sent','failed','cancelled','needs_attention','uncertain')),
    workspace_updated_at TEXT,
    context_packet_ref TEXT,
    ticket_id TEXT REFERENCES support_tickets(ticket_id),
    work_item_id TEXT REFERENCES work_items(work_item_id),
    claimed_by TEXT,
    claimed_at TEXT,
    pattern_used TEXT,
    sent_version TEXT,
    sent_at TEXT,
    scheduled_send_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_drafts_claimed_by
    ON drafts(claimed_by, claimed_at);

CREATE TABLE IF NOT EXISTS draft_status_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id TEXT NOT NULL REFERENCES drafts(draft_id),
    from_status TEXT,
    to_status TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT 'system',
    surface TEXT,
    changed_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_draft_status_history_draft
    ON draft_status_history(draft_id, changed_at DESC);

-- Approval-bound external and high-impact actions
CREATE TABLE IF NOT EXISTS action_intents (
    action_id TEXT PRIMARY KEY,
    operator_id TEXT NOT NULL DEFAULT 'default_operator',
    action_type TEXT NOT NULL,
    risk_class TEXT NOT NULL CHECK (risk_class IN ('low','medium','high','critical')),
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    target_version TEXT,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN (
            'pending','approved','rejected','executing','executed',
            'failed','expired','invalidated','uncertain'
        )
    ),
    created_by_surface TEXT NOT NULL,
    approved_by TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL,
    approved_at TEXT,
    executed_at TEXT,
    result_ref TEXT,
    error TEXT,
    idempotency_key TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_action_intents_status
    ON action_intents(operator_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_action_intents_target
    ON action_intents(target_type, target_id, created_at DESC);

-- Internal follow-ups
CREATE TABLE IF NOT EXISTS internal_followups (
    followup_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    owner_contact_id TEXT,
    description TEXT NOT NULL,
    due_date TEXT,
    reminder_due_at TEXT,
    action_revision INTEGER NOT NULL DEFAULT 0,
    owner_side TEXT CHECK(owner_side IN ('Internal','External','Shared')),
    due_date_basis TEXT NOT NULL DEFAULT 'legacy_unknown'
        CHECK(due_date_basis IN ('legacy_unknown','operator_entered','reported_commitment','none')),
    status TEXT DEFAULT 'open'
        CHECK (status IN ('open','completed','overdue','cancelled')),
    escalation_level INTEGER DEFAULT 0,
    linked_message_ref TEXT,
    created_by TEXT DEFAULT 'operator'
        CHECK (created_by IN ('auto_resolved', 'operator_confirmed', 'operator')),
    resolved_at TEXT,
    resolution_note TEXT,
    reminder_sent_at TEXT,
    related_external_contact_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_followups_project
    ON internal_followups(project_id, status);

-- Proposed updates (audit trail of all Write Layer decisions)
CREATE TABLE IF NOT EXISTS proposed_updates (
    update_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    target_project_id TEXT,
    operation_id TEXT,
    proposed_changes TEXT NOT NULL,
    task_type TEXT NOT NULL,
    confidence REAL NOT NULL,
    risk_level TEXT NOT NULL,
    requires_operator_review INTEGER NOT NULL DEFAULT 0,
    decision_outcome TEXT
        CHECK (decision_outcome IN ('accepted','rejected','needs_review','deferred')),
    rejection_reasons TEXT,
    review_card_id TEXT,
    re_trigger_condition TEXT,
    re_trigger_count INTEGER DEFAULT 0,
    operator_id TEXT DEFAULT 'default_operator',
    decided_by TEXT,
    assessment_json TEXT,
    policy_version TEXT,
    source_type TEXT,
    source_ref TEXT,
    decided_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_proposed_updates_project
    ON proposed_updates(project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_proposed_updates_deferred
    ON proposed_updates(decision_outcome)
    WHERE decision_outcome = 'deferred';
CREATE INDEX IF NOT EXISTS idx_proposed_updates_operation
    ON proposed_updates(operation_id, created_at DESC);

-- Deferred queue (pointer to deferred proposed_updates)
CREATE TABLE IF NOT EXISTS deferred_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposed_update_id TEXT NOT NULL REFERENCES proposed_updates(update_id),
    re_trigger_condition TEXT NOT NULL,
    deferred_at TEXT NOT NULL DEFAULT (datetime('now')),
    next_check_at TEXT,
    status TEXT DEFAULT 'waiting'
        CHECK (status IN ('waiting','triggered','dismissed','expired')),
    triggered_at TEXT
);

-- Unified activity log (append-only, immutable)
CREATE TABLE IF NOT EXISTS activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT,
    actor TEXT NOT NULL,
    surface TEXT,
    action_type TEXT NOT NULL,
    object_ref TEXT,
    summary TEXT,
    metadata_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_activity_log_project
    ON activity_log(project_id, created_at DESC);

-- Telegram sessions (schema ready for Phase 2)
CREATE TABLE IF NOT EXISTS telegram_sessions (
    session_id TEXT PRIMARY KEY,
    flow_type TEXT NOT NULL,
    originating_ref TEXT,
    operator_id TEXT DEFAULT 'default_operator',
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_action_at TEXT NOT NULL DEFAULT (datetime('now')),
    timeout_at TEXT,
    status TEXT DEFAULT 'active'
        CHECK (status IN ('active','completed','timed_out','cancelled')),
    intermediate_data TEXT
);
CREATE INDEX IF NOT EXISTS idx_telegram_sessions_flow_status
    ON telegram_sessions(flow_type, status);

-- Telegram message history (raw inbound/outbound traffic)
CREATE TABLE IF NOT EXISTS telegram_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    telegram_message_id INTEGER NOT NULL,
    direction TEXT NOT NULL
        CHECK (direction IN ('inbound','outbound')),
    message_kind TEXT NOT NULL DEFAULT 'text',
    text TEXT,
    callback_data TEXT,
    reply_to_telegram_message_id INTEGER,
    project_id TEXT,
    session_id TEXT REFERENCES telegram_sessions(session_id),
    operator_id TEXT DEFAULT 'default_operator',
    metadata_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(chat_id, telegram_message_id)
);
CREATE INDEX IF NOT EXISTS idx_telegram_messages_project
    ON telegram_messages(project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_telegram_messages_session
    ON telegram_messages(session_id, created_at DESC);

-- Link Telegram messages to backend objects
CREATE TABLE IF NOT EXISTS message_object_map (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    telegram_message_id INTEGER NOT NULL,
    object_type TEXT NOT NULL,
    object_ref TEXT NOT NULL,
    relation_type TEXT NOT NULL DEFAULT 'primary',
    project_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(chat_id, telegram_message_id, object_type, object_ref, relation_type)
);
CREATE INDEX IF NOT EXISTS idx_message_object_map_message
    ON message_object_map(chat_id, telegram_message_id);
CREATE INDEX IF NOT EXISTS idx_message_object_map_object
    ON message_object_map(object_type, object_ref);

-- Processed emails (migrated reference data)
CREATE TABLE IF NOT EXISTS processed_emails (
    email_id TEXT PRIMARY KEY,
    project_id TEXT,
    processed_at TEXT,
    received_at TEXT,
    sent_at TEXT,
    result TEXT,
    thread_fp TEXT,
    subject TEXT,
    sender_email TEXT,
    direction TEXT,
    thread_id TEXT,
    in_reply_to TEXT,
    references_chain TEXT,
    cc TEXT,
    to_recipients_json TEXT NOT NULL DEFAULT '[]',
    cc_recipients_json TEXT NOT NULL DEFAULT '[]',
    bcc_recipients_json TEXT NOT NULL DEFAULT '[]',
    ticket_id TEXT REFERENCES support_tickets(ticket_id),
    body_snippet TEXT,
    knowledge_mined INTEGER DEFAULT 0,
    knowledge_mining_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (knowledge_mining_status IN ('pending','processing','mined','failed')),
    knowledge_mining_attempts INTEGER NOT NULL DEFAULT 0,
    knowledge_mining_error TEXT,
    knowledge_mining_lease_owner TEXT,
    knowledge_mining_lease_expires_at TEXT,
    knowledge_mined_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_processed_emails_project_direction
    ON processed_emails(project_id, direction);
CREATE INDEX IF NOT EXISTS idx_processed_emails_thread_fp
    ON processed_emails(thread_fp);
CREATE INDEX IF NOT EXISTS idx_processed_emails_project_received
    ON processed_emails(project_id, received_at DESC);

-- Email knowledge mining log
CREATE TABLE IF NOT EXISTS email_knowledge_log (
    entry_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    thread_id TEXT,
    project_id TEXT,
    ticket_id TEXT,
    knowledge_type TEXT NOT NULL CHECK (knowledge_type IN ('domain_fact', 'problem_solution', 'process_step')),
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    confidence TEXT NOT NULL DEFAULT 'medium' CHECK (confidence IN ('high', 'medium', 'low')),
    visibility TEXT NOT NULL DEFAULT 'public' CHECK (visibility IN ('public', 'internal_only')),
    wiki_path TEXT,
    source_emails TEXT NOT NULL DEFAULT '[]',
    mining_run_id TEXT NOT NULL,
    domain TEXT DEFAULT 'general',
    usage_count INTEGER DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('draft', 'active', 'archived'))
);
CREATE INDEX IF NOT EXISTS idx_email_knowledge_log_created
    ON email_knowledge_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_email_knowledge_log_project
    ON email_knowledge_log(project_id);

-- Wiki compiler queue: nuggets waiting to become draft wiki pages
CREATE TABLE IF NOT EXISTS wiki_compiler_queue (
    queue_id TEXT PRIMARY KEY,
    entry_id TEXT NOT NULL UNIQUE REFERENCES email_knowledge_log(entry_id),
    domain TEXT,
    topic TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending','compiling','compiled','skipped','failed')),
    skip_reason TEXT,
    compiled_path TEXT,
    error TEXT,
    enqueued_at TEXT NOT NULL,
    compiled_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_wiki_compiler_queue_state
    ON wiki_compiler_queue(state, topic, enqueued_at);

-- Wiki usage log (append-only, tracks knowledge graph access patterns)
CREATE TABLE IF NOT EXISTS wiki_usage_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    wiki_path TEXT NOT NULL,
    used_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_wiki_usage_log_path ON wiki_usage_log(wiki_path);

-- Inbound replies (Blueprint Section 27.3 — detected client replies)
CREATE TABLE IF NOT EXISTS inbound_replies (
    reply_id TEXT PRIMARY KEY,
    thread_id TEXT,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    from_contact_id TEXT,
    received_at TEXT NOT NULL,
    subject TEXT,
    body_text TEXT,
    original_draft_id TEXT,
    summary TEXT,
    suggested_action TEXT,
    confidence REAL DEFAULT 0.0,
    requested_docs_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_inbound_replies_project
    ON inbound_replies(project_id, received_at DESC);

-- LLM call traces (agent observability)
CREATE TABLE IF NOT EXISTS llm_traces (
    trace_id TEXT PRIMARY KEY,
    llm_call_id TEXT,
    task TEXT NOT NULL,
    model TEXT NOT NULL,
    tokens_in INTEGER DEFAULT 0,
    tokens_out INTEGER DEFAULT 0,
    latency_ms INTEGER DEFAULT 0,
    output_valid INTEGER DEFAULT 1,
    error_class TEXT CHECK (error_class IN ('none','infra','schema','guard','timeout') OR error_class IS NULL),
    error_message TEXT,
    decision_outcome TEXT,
    prompt_hash TEXT,
    per_guard_results TEXT,
    trace_metadata TEXT,
    run_id TEXT,
    turn_id TEXT,
    round_index INTEGER,
    ttft_ms REAL,
    retry_count INTEGER DEFAULT 0,
    failover_from TEXT,
    cache_hit INTEGER,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    request_event_id TEXT,
    response_event_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_llm_traces_created_at
    ON llm_traces(created_at);
CREATE INDEX IF NOT EXISTS idx_llm_traces_task
    ON llm_traces(task);
CREATE INDEX IF NOT EXISTS idx_llm_traces_run_id
    ON llm_traces(run_id);

-- Run-oriented trace explorer
CREATE TABLE IF NOT EXISTS trace_runs (
    run_id TEXT PRIMARY KEY, schema_version INTEGER NOT NULL DEFAULT 1, trace_id TEXT, turn_id TEXT, thread_id TEXT,
    surface TEXT NOT NULL, task TEXT NOT NULL, project_id TEXT, session_id TEXT, chat_id INTEGER, operator_id TEXT,
    status TEXT NOT NULL DEFAULT 'running', anomaly_flags_json TEXT, exit_reason TEXT, started_at TEXT NOT NULL,
    ended_at TEXT, duration_ms INTEGER, metadata_json TEXT, implicit INTEGER DEFAULT 0, component TEXT,
    component_version TEXT, persistence_status TEXT
);
CREATE INDEX IF NOT EXISTS idx_trace_runs_started ON trace_runs(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_trace_runs_filter ON trace_runs(surface, task, status, project_id);
CREATE INDEX IF NOT EXISTS idx_trace_runs_trace_id ON trace_runs(trace_id);
CREATE TABLE IF NOT EXISTS trace_events (
    event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES trace_runs(run_id) ON DELETE CASCADE,
    schema_version INTEGER NOT NULL DEFAULT 1, trace_id TEXT, turn_id TEXT, thread_id TEXT, sequence INTEGER NOT NULL,
    event_type TEXT NOT NULL, round_index INTEGER, llm_trace_id TEXT REFERENCES llm_traces(trace_id), llm_call_id TEXT,
    tool_call_id TEXT, tool_name TEXT, status TEXT NOT NULL, duration_ms REAL, component TEXT, component_version TEXT,
    parent_event_id TEXT, parent_span_id TEXT, metadata_json TEXT, payload_ciphertext TEXT, payload_nonce TEXT,
    payload_key_version TEXT, payload_hash TEXT, created_at TEXT NOT NULL, UNIQUE(run_id, sequence)
);
CREATE INDEX IF NOT EXISTS idx_trace_events_run ON trace_events(run_id, sequence);
CREATE TABLE IF NOT EXISTS trace_diagnostics (
    diagnostic_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES trace_runs(run_id) ON DELETE CASCADE, trigger TEXT NOT NULL,
    status TEXT NOT NULL, severity TEXT, root_cause TEXT, summary TEXT, evidence_json TEXT, recommendations_json TEXT,
    diagnostic_model TEXT, diagnostic_trace_id TEXT REFERENCES llm_traces(trace_id), created_at TEXT NOT NULL, completed_at TEXT,
    UNIQUE(run_id, trigger)
);
CREATE INDEX IF NOT EXISTS idx_trace_diagnostics_run ON trace_diagnostics(run_id, created_at DESC);


-- Closing checklist (Blueprint Section 30/31 — structured per-item sign-off)
CREATE TABLE IF NOT EXISTS closing_checklist (
    item_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    item_key TEXT,
    label TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'confirmed', 'dismissed')),
    dismissed_reason TEXT,
    confirmed_at TEXT,
    evidence_ref TEXT,
    resolution_note TEXT,
    resolved_by TEXT,
    resolved_at TEXT,
    initiated_at TEXT NOT NULL DEFAULT (datetime('now')),
    initiated_by TEXT NOT NULL DEFAULT 'operator',
    operator_sign_off TEXT,
    signed_off_at TEXT,
    sign_off_note TEXT,
    acknowledgements_json TEXT,
    closed_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_closing_checklist_project_key
    ON closing_checklist(project_id, item_key)
    WHERE item_key IS NOT NULL;

-- Operator interaction log (operator action audit trail for every card push/response)
CREATE TABLE IF NOT EXISTS operator_interaction_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    card_type TEXT,
    card_profile TEXT,
    project_id TEXT,
    pushed_at TEXT NOT NULL,
    operator_response TEXT,
    responded_at TEXT,
    timeout_triggered INTEGER DEFAULT 0,
    telegram_message_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_interaction_log_msg
    ON operator_interaction_log(telegram_message_id);
CREATE INDEX IF NOT EXISTS idx_interaction_log_project
    ON operator_interaction_log(project_id, pushed_at DESC);

-- AI feedback (operator corrections on AI outputs)
CREATE TABLE IF NOT EXISTS ai_feedback (
    feedback_id TEXT PRIMARY KEY,
    item_type TEXT NOT NULL
        CHECK (item_type IN ('draft','triage','reply_analysis','status_answer')),
    item_id TEXT NOT NULL,
    project_id TEXT REFERENCES projects(project_id),
    feedback_kind TEXT NOT NULL
        CHECK (feedback_kind IN ('correct','wrong','edited')),
    category TEXT,
    operator_note TEXT,
    surface TEXT,
    trace_id TEXT,
    run_id TEXT,
    turn_id TEXT,
    output_revision_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ai_feedback_trace_id ON ai_feedback(trace_id);
CREATE INDEX IF NOT EXISTS idx_ai_feedback_run_id ON ai_feedback(run_id);
CREATE INDEX IF NOT EXISTS idx_ai_feedback_turn_id ON ai_feedback(turn_id);

-- Support tickets (non-project technical issues)
CREATE TABLE IF NOT EXISTS support_tickets (
    ticket_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    category TEXT NOT NULL CHECK (category IN (
        'fix_session','network','trade','post_trade',
        'cert','config','access','monitoring','outreach','other'
    )),
    severity TEXT NOT NULL DEFAULT 'medium' CHECK (severity IN (
        'critical','high','medium','low'
    )),
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN (
        'open','investigating','waiting_external','waiting_internal',
        'resolved','closed'
    )),
    source_type TEXT NOT NULL DEFAULT 'email' CHECK (source_type IN (
        'email','telegram','manual'
    )),
    related_project_id TEXT REFERENCES projects(project_id),
    counterparty TEXT,
    counterparty_org_id TEXT REFERENCES organisations(org_id),
    reporter_email TEXT,
    reporter_contact_id TEXT REFERENCES contacts(contact_id),
    description TEXT,
    root_cause TEXT,
    resolution TEXT,
    assigned_team TEXT,
    escalated_to TEXT,
    reported_at TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at TEXT,
    closed_at TEXT,
    sla_target_hours REAL,
    resolution_pattern_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    reply_draft_text    TEXT,
    draft_generated_at  TEXT,
    ai_summary_json     TEXT,
    thread_anchor       TEXT,
    work_item_id        TEXT
);
CREATE INDEX IF NOT EXISTS idx_support_tickets_status
    ON support_tickets(status);
CREATE INDEX IF NOT EXISTS idx_support_tickets_counterparty_org
    ON support_tickets(counterparty_org_id);
CREATE INDEX IF NOT EXISTS idx_support_tickets_project
    ON support_tickets(related_project_id);

-- Ticket evidence (append-only)
CREATE TABLE IF NOT EXISTS ticket_evidence (
    evidence_id TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL REFERENCES support_tickets(ticket_id),
    evidence_type TEXT NOT NULL CHECK (evidence_type IN (
        'email','note','log_snippet','screenshot','resolution'
    )),
    content_summary TEXT NOT NULL,
    content_text TEXT,
    source_ref TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ticket_evidence_ticket
    ON ticket_evidence(ticket_id);

-- Resolution patterns (learned from operator handling)
CREATE TABLE IF NOT EXISTS resolution_patterns (
    pattern_id TEXT PRIMARY KEY,
    category TEXT,
    subcategory TEXT,
    trigger_keywords TEXT DEFAULT '[]',
    diagnosis_steps TEXT DEFAULT '[]',
    resolution_action TEXT,
    escalation_target TEXT,
    typical_draft_template TEXT,
    source TEXT NOT NULL DEFAULT 'operator'
        CHECK (source IN ('operator','auto_extracted')),
    times_used INTEGER DEFAULT 1,
    times_confirmed INTEGER DEFAULT 0,
    confidence REAL DEFAULT 0.5,
    last_used_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_resolution_patterns_category
    ON resolution_patterns(category);
CREATE INDEX IF NOT EXISTS idx_resolution_patterns_confidence
    ON resolution_patterns(confidence);

CREATE TABLE IF NOT EXISTS support_chat_messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id     TEXT NOT NULL REFERENCES support_tickets(ticket_id) ON DELETE CASCADE,
    owner_user_id TEXT NOT NULL DEFAULT 'default_operator',
    turn_id       TEXT,
    role          TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
    content       TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_support_chat_ticket
    ON support_chat_messages(ticket_id, owner_user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_support_chat_turn
    ON support_chat_messages(ticket_id, owner_user_id, turn_id, created_at);

CREATE TABLE IF NOT EXISTS session_summaries (
    session_id   TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(8)))),
    chat_id      INTEGER NOT NULL,
    started_at   TEXT NOT NULL DEFAULT (datetime('now')),
    ended_at     TEXT NOT NULL DEFAULT (datetime('now')),
    summary_text TEXT NOT NULL,
    projects_discussed TEXT,
    decisions_made     TEXT,
    open_questions     TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_session_summaries_chat
    ON session_summaries(chat_id, ended_at DESC);

-- Immutable learning observation ledger (capture-only; never active memory)
CREATE TABLE IF NOT EXISTS learning_observations (
    observation_id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL CHECK (source_type IN ('conversation_flush','operator_feedback','draft_edit','email_sent','client_reply','tool_failure','manual_memory','wiki_ingestion','migration')),
    source_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    operator_id TEXT NOT NULL,
    project_id TEXT,
    contact_id TEXT,
    counterparty_id TEXT,
    artifact_type TEXT,
    artifact_id TEXT,
    trace_id TEXT,
    output_revision_id TEXT,
    raw_payload_json TEXT NOT NULL DEFAULT '{}',
    normalized_text TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    sensitivity TEXT NOT NULL CHECK (sensitivity IN ('public','internal','confidential','restricted')),
    trust_tier TEXT NOT NULL CHECK (trust_tier IN ('untrusted','external','system','operator')),
    occurred_at TEXT NOT NULL,
    is_tombstone INTEGER NOT NULL DEFAULT 0 CHECK (is_tombstone IN (0,1)),
    tombstone_of_observation_id TEXT,
    tombstone_reason TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(source_type, source_id, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_learning_observations_scope
    ON learning_observations(operator_id, project_id, contact_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_learning_observations_artifact
    ON learning_observations(artifact_type, artifact_id, occurred_at DESC);
CREATE TRIGGER IF NOT EXISTS trg_learning_observations_no_update
BEFORE UPDATE ON learning_observations
BEGIN
    SELECT RAISE(ABORT, 'learning_observations is append-only');
END;
CREATE TRIGGER IF NOT EXISTS trg_learning_observations_no_delete
BEFORE DELETE ON learning_observations
BEGIN
    SELECT RAISE(ABORT, 'learning_observations is append-only');
END;

-- Exact immutable output, exposure, and outcome attribution ledgers
CREATE TABLE IF NOT EXISTS output_revisions (
    output_revision_id TEXT PRIMARY KEY,
    operator_id TEXT NOT NULL,
    surface TEXT NOT NULL CHECK (surface IN ('chat','web','draft','advisory','tool')),
    artifact_type TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    trace_id TEXT,
    content_hash TEXT NOT NULL,
    prior_output_revision_id TEXT REFERENCES output_revisions(output_revision_id),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(surface, artifact_type, artifact_id, revision)
);
CREATE TABLE IF NOT EXISTS memory_exposures (
    exposure_id TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL,
    output_revision_id TEXT NOT NULL REFERENCES output_revisions(output_revision_id),
    operator_id TEXT NOT NULL,
    surface TEXT NOT NULL CHECK (surface IN ('chat','web','draft','advisory','tool')),
    source_surface TEXT NOT NULL CHECK (source_surface IN ('chat','web','draft','advisory','tool')),
    task_type TEXT,
    project_id TEXT,
    fact_id TEXT NOT NULL,
    fact_version INTEGER NOT NULL CHECK (fact_version >= 1),
    rendered_hash TEXT NOT NULL,
    rank INTEGER NOT NULL CHECK (rank >= 1),
    token_count INTEGER NOT NULL CHECK (token_count >= 0),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(trace_id, output_revision_id, fact_id, fact_version)
);
CREATE INDEX IF NOT EXISTS idx_memory_exposures_exact
    ON memory_exposures(operator_id, surface, trace_id, output_revision_id);
CREATE TRIGGER IF NOT EXISTS trg_memory_exposures_revision_match
BEFORE INSERT ON memory_exposures
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM output_revisions r
        WHERE r.output_revision_id=NEW.output_revision_id
          AND r.trace_id=NEW.trace_id AND r.operator_id=NEW.operator_id
          AND r.surface=NEW.surface
    ) THEN RAISE(ABORT, 'memory exposure/revision mismatch') END;
END;
CREATE TABLE IF NOT EXISTS learning_outcomes (
    outcome_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL,
    exposure_id TEXT NOT NULL REFERENCES memory_exposures(exposure_id),
    trace_id TEXT NOT NULL,
    output_revision_id TEXT NOT NULL REFERENCES output_revisions(output_revision_id),
    operator_id TEXT NOT NULL,
    surface TEXT NOT NULL CHECK (surface IN ('chat','web','draft','advisory','tool')),
    fact_id TEXT NOT NULL,
    fact_version INTEGER NOT NULL CHECK (fact_version >= 1),
    feedback_id TEXT,
    outcome_type TEXT NOT NULL CHECK (outcome_type IN ('positive','negative','correction','send','client_reply')),
    outcome_value REAL NOT NULL,
    reason TEXT,
    actor_id TEXT NOT NULL,
    feedback_scope TEXT CHECK (feedback_scope IS NULL OR feedback_scope = 'all_rendered'),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(idempotency_key, exposure_id)
);
CREATE TRIGGER IF NOT EXISTS trg_learning_outcomes_exact_match
BEFORE INSERT ON learning_outcomes
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM output_revisions r
        JOIN memory_exposures e ON e.exposure_id=NEW.exposure_id
        WHERE r.output_revision_id=NEW.output_revision_id
          AND r.trace_id=NEW.trace_id AND r.operator_id=NEW.operator_id
          AND r.surface=NEW.surface
          AND e.output_revision_id=NEW.output_revision_id
          AND e.trace_id=NEW.trace_id AND e.operator_id=NEW.operator_id
          AND e.surface=NEW.surface AND e.fact_id=NEW.fact_id
          AND e.fact_version=NEW.fact_version
    ) THEN RAISE(ABORT, 'learning outcome attribution mismatch') END;
END;
CREATE INDEX IF NOT EXISTS idx_learning_outcomes_fact
    ON learning_outcomes(fact_id, fact_version, created_at);
CREATE TRIGGER IF NOT EXISTS trg_output_revisions_no_update BEFORE UPDATE ON output_revisions
BEGIN SELECT RAISE(ABORT, 'output_revisions is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_output_revisions_no_delete BEFORE DELETE ON output_revisions
BEGIN SELECT RAISE(ABORT, 'output_revisions is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_memory_exposures_no_update BEFORE UPDATE ON memory_exposures
BEGIN SELECT RAISE(ABORT, 'memory_exposures is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_memory_exposures_no_delete BEFORE DELETE ON memory_exposures
BEGIN SELECT RAISE(ABORT, 'memory_exposures is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_learning_outcomes_no_update BEFORE UPDATE ON learning_outcomes
BEGIN SELECT RAISE(ABORT, 'learning_outcomes is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_learning_outcomes_no_delete BEFORE DELETE ON learning_outcomes
BEGIN SELECT RAISE(ABORT, 'learning_outcomes is append-only'); END;

-- Agent Notes (LLM-curated personal memory)
CREATE TABLE IF NOT EXISTS agent_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note_key TEXT UNIQUE NOT NULL,
    note_value TEXT NOT NULL,
    note_type TEXT NOT NULL DEFAULT 'knowledge' CHECK (note_type IN ('knowledge', 'diary', 'preference', 'pattern', 'correction')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Operator memory (unified long-term store — memory reform)
CREATE TABLE IF NOT EXISTS operator_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operator_id TEXT NOT NULL,
    scope TEXT NOT NULL CHECK (scope IN (
        'global', 'project', 'chat', 'counterparty', 'contact',
        'organisation', 'work_item', 'topic', 'calendar_event'
    )),
    scope_id TEXT NOT NULL DEFAULT '*',
    memory_type TEXT NOT NULL CHECK (
        memory_type IN ('semantic', 'procedural', 'forbidden', 'episodic_snippet')
    ),
    memory_key TEXT NOT NULL,
    memory_value TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'operator'
        CHECK (source IN ('operator', 'memory_flush', 'dashboard', 'migration', 'eval')),
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (operator_id, scope, scope_id, memory_type, memory_key)
);
CREATE INDEX IF NOT EXISTS idx_operator_memory_lookup
    ON operator_memory (operator_id, scope, scope_id, memory_type, active);

CREATE TABLE IF NOT EXISTS thread_artifacts (
    artifact_id TEXT PRIMARY KEY,
    operator_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    project_id TEXT,
    content_json TEXT NOT NULL,
    char_count INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_thread_artifacts_thread
    ON thread_artifacts (operator_id, thread_id, created_at DESC);

-- Specialist delegate sessions (resume_from continuation)
CREATE TABLE IF NOT EXISTS specialist_sessions (
    session_id TEXT PRIMARY KEY,
    operator_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    skill_id TEXT NOT NULL,
    specialist_type TEXT NOT NULL,
    project_id TEXT,
    parent_session_id TEXT,
    messages_json TEXT NOT NULL,
    tool_history_json TEXT,
    checkpoint_json TEXT NOT NULL DEFAULT '{}',
    checkpoint_seq INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'completed'
        CHECK (status IN (
            'in_progress', 'completed', 'partial', 'timed_out',
            'exhausted', 'failed', 'unverified'
        )),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_specialist_sessions_thread
    ON specialist_sessions (operator_id, thread_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_specialist_sessions_expires
    ON specialist_sessions (expires_at);

-- Project corrections (agent self-correction memory — L3)
CREATE TABLE IF NOT EXISTS project_corrections (
    correction_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    field TEXT NOT NULL,
    wrong_value TEXT NOT NULL,
    correct_value TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'agent_self_correction',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_project_corrections_project
    ON project_corrections(project_id, created_at DESC);

-- Project chat messages (dashboard agent chat)
CREATE TABLE IF NOT EXISTS project_chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL DEFAULT 'default_operator',
    turn_id TEXT,
    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_project_chat_owner
    ON project_chat_messages(project_id, owner_user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_project_chat_turn
    ON project_chat_messages(project_id, owner_user_id, turn_id, created_at);

-- Work items supertype — unified envelope for projects, tickets, outreach, standalone
CREATE TABLE IF NOT EXISTS work_items (
    work_item_id  TEXT PRIMARY KEY,
    work_item_type TEXT NOT NULL CHECK (
        work_item_type IN (
            'project','support_ticket','outreach','standalone',
            'task','commitment','waiting_for','reminder'
        )
    ),
    title         TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'open' CHECK (
        status IN ('open','resolved','closed','cancelled')
    ),
    priority      TEXT DEFAULT 'medium' CHECK (
        priority IN ('critical','high','medium','low')
    ),
    entity_table  TEXT CHECK (
        entity_table IN ('projects','support_tickets','personal_work_items')
        OR entity_table IS NULL
    ),
    entity_id     TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_work_items_status ON work_items(status);
CREATE INDEX IF NOT EXISTS idx_work_items_type   ON work_items(work_item_type);
CREATE INDEX IF NOT EXISTS idx_work_items_entity ON work_items(entity_table, entity_id);

CREATE TRIGGER IF NOT EXISTS trg_work_items_updated_at
AFTER UPDATE ON work_items
BEGIN
    UPDATE work_items SET updated_at = datetime('now') WHERE work_item_id = NEW.work_item_id;
END;

-- Personal tasks, commitments, waiting-for items, and reminders
CREATE TABLE IF NOT EXISTS personal_work_items (
    work_item_id TEXT PRIMARY KEY REFERENCES work_items(work_item_id),
    operator_id TEXT NOT NULL DEFAULT 'default_operator',
    kind TEXT NOT NULL CHECK (kind IN ('task','commitment','waiting_for','reminder')),
    description TEXT,
    owner_type TEXT NOT NULL DEFAULT 'operator' CHECK (
        owner_type IN ('operator','contact','team','unknown')
    ),
    owner_ref TEXT,
    related_entity_type TEXT,
    related_entity_id TEXT,
    due_at TEXT,
    planned_start_at TEXT,
    duration_minutes INTEGER,
    remind_at TEXT,
    snoozed_until TEXT,
    recurrence_rule TEXT,
    source_type TEXT NOT NULL DEFAULT 'manual' CHECK (
        source_type IN ('manual','email','meeting','telegram','agent')
    ),
    source_ref TEXT,
    evidence_text TEXT,
    confidence REAL NOT NULL DEFAULT 1.0 CHECK (confidence >= 0 AND confidence <= 1),
    review_status TEXT NOT NULL DEFAULT 'not_required' CHECK (
        review_status IN ('proposed','confirmed','rejected','not_required')
    ),
    dedup_key TEXT,
    completed_at TEXT,
    resolution_note TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_personal_work_items_due
    ON personal_work_items(operator_id, review_status, due_at);
CREATE INDEX IF NOT EXISTS idx_personal_work_items_related
    ON personal_work_items(related_entity_type, related_entity_id);
CREATE INDEX IF NOT EXISTS idx_personal_work_items_remind
    ON personal_work_items(remind_at, snoozed_until);
CREATE UNIQUE INDEX IF NOT EXISTS idx_personal_work_items_dedup
    ON personal_work_items(operator_id, dedup_key)
    WHERE dedup_key IS NOT NULL AND review_status != 'rejected';

-- User-created reminders and generic assistant schedules
CREATE TABLE IF NOT EXISTS assistant_schedules (
    schedule_id TEXT PRIMARY KEY,
    operator_id TEXT NOT NULL DEFAULT 'default_operator',
    title TEXT NOT NULL,
    trigger_type TEXT NOT NULL CHECK (trigger_type IN ('at','rrule','interval')),
    trigger_value TEXT NOT NULL,
    timezone TEXT NOT NULL DEFAULT 'Asia/Hong_Kong',
    command_type TEXT NOT NULL,
    command_payload_json TEXT NOT NULL DEFAULT '{}',
    channel TEXT NOT NULL DEFAULT 'telegram' CHECK (channel IN ('telegram','dashboard')),
    urgency TEXT NOT NULL DEFAULT 'normal' CHECK (urgency IN ('normal','urgent','emergency')),
    enabled INTEGER NOT NULL DEFAULT 1,
    dedup_key TEXT,
    next_run_at TEXT,
    last_run_at TEXT,
    intent_version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_assistant_schedules_due
    ON assistant_schedules(enabled, next_run_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_assistant_schedules_dedup
    ON assistant_schedules(operator_id, dedup_key)
    WHERE dedup_key IS NOT NULL AND enabled = 1;

CREATE TABLE IF NOT EXISTS assistant_schedule_runs (
    run_id TEXT PRIMARY KEY,
    schedule_id TEXT NOT NULL REFERENCES assistant_schedules(schedule_id),
    scheduled_for TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('claimed','sent','completed','failed','missed')),
    attempt_count INTEGER NOT NULL DEFAULT 1,
    intent_version INTEGER NOT NULL DEFAULT 0,
    result_json TEXT,
    error TEXT,
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT,
    UNIQUE(schedule_id, scheduled_for)
);
CREATE INDEX IF NOT EXISTS idx_assistant_schedule_runs_schedule
    ON assistant_schedule_runs(schedule_id, started_at DESC);

-- Shared Telegram/Dashboard personal brief snapshots
CREATE TABLE IF NOT EXISTS personal_brief_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    operator_id TEXT NOT NULL DEFAULT 'default_operator',
    generated_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    feedback_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_personal_brief_snapshots_operator
    ON personal_brief_snapshots(operator_id, generated_at DESC);

-- Actionable analysis layered on top of persisted inbound email
CREATE TABLE IF NOT EXISTS inbox_insights (
    insight_id TEXT PRIMARY KEY,
    email_id TEXT NOT NULL REFERENCES processed_emails(email_id),
    operator_id TEXT NOT NULL DEFAULT 'default_operator',
    priority TEXT NOT NULL CHECK (priority IN ('low','normal','high','urgent')),
    priority_reasons_json TEXT NOT NULL DEFAULT '[]',
    action_required INTEGER NOT NULL DEFAULT 0,
    request_summary TEXT,
    suggested_action TEXT,
    due_at TEXT,
    proposed_owner TEXT,
    related_entities_json TEXT NOT NULL DEFAULT '[]',
    evidence_spans_json TEXT NOT NULL DEFAULT '[]',
    confidence REAL NOT NULL DEFAULT 0.0 CHECK (confidence >= 0 AND confidence <= 1),
    review_status TEXT NOT NULL DEFAULT 'not_required' CHECK (
        review_status IN ('proposed','confirmed','rejected','not_required')
    ),
    category TEXT NOT NULL DEFAULT 'fyi',
    suppressed INTEGER NOT NULL DEFAULT 0,
    suppression_reason TEXT,
    dedup_key TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(operator_id, dedup_key)
);
CREATE INDEX IF NOT EXISTS idx_inbox_insights_priority
    ON inbox_insights(operator_id, priority, action_required, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_inbox_insights_email
    ON inbox_insights(email_id);

-- Read-only Microsoft Graph calendar cache
CREATE TABLE IF NOT EXISTS calendar_events (
    calendar_event_id TEXT PRIMARY KEY,
    operator_id TEXT NOT NULL DEFAULT 'default_operator',
    calendar_id TEXT NOT NULL,
    subject TEXT NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    original_timezone TEXT,
    location TEXT,
    organizer_email TEXT,
    attendees_json TEXT NOT NULL DEFAULT '[]',
    is_online_meeting INTEGER NOT NULL DEFAULT 0,
    join_url TEXT,
    show_as TEXT,
    sensitivity TEXT,
    body_preview TEXT,
    last_modified_at TEXT,
    source_etag TEXT,
    raw_hash TEXT NOT NULL,
    synced_at TEXT NOT NULL,
    cancelled INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_calendar_events_time
    ON calendar_events(operator_id, start_at, end_at);

CREATE TABLE IF NOT EXISTS calendar_sync_state (
    operator_id TEXT NOT NULL,
    calendar_id TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT 'msgraph',
    delta_token TEXT,
    last_success_at TEXT,
    last_error_at TEXT,
    last_error TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY(operator_id, calendar_id, provider)
);

-- Shared, auditable meeting preparation snapshots
CREATE TABLE IF NOT EXISTS meeting_prep_snapshots (
    prep_id TEXT PRIMARY KEY,
    operator_id TEXT NOT NULL DEFAULT 'default_operator',
    calendar_event_id TEXT NOT NULL REFERENCES calendar_events(calendar_event_id),
    event_raw_hash TEXT NOT NULL,
    project_id TEXT REFERENCES projects(project_id),
    payload_json TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    UNIQUE(operator_id, calendar_event_id, event_raw_hash)
);
CREATE INDEX IF NOT EXISTS idx_meeting_prep_event
    ON meeting_prep_snapshots(operator_id, calendar_event_id, generated_at DESC);

-- Immutable meeting-note ingestion with evidence-backed proposed work items
CREATE TABLE IF NOT EXISTS meeting_ingestions (
    ingestion_id TEXT PRIMARY KEY,
    operator_id TEXT NOT NULL DEFAULT 'default_operator',
    calendar_event_id TEXT NOT NULL REFERENCES calendar_events(calendar_event_id),
    source_hash TEXT NOT NULL,
    source_text TEXT NOT NULL,
    extracted_json TEXT NOT NULL DEFAULT '{}',
    work_item_ids_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    UNIQUE(operator_id, calendar_event_id, source_hash)
);
CREATE INDEX IF NOT EXISTS idx_meeting_ingestions_event
    ON meeting_ingestions(operator_id, calendar_event_id, created_at DESC);

-- Owned, expiring, optimistic-concurrency protected agent operations
CREATE TABLE IF NOT EXISTS pending_operations (
    operation_id TEXT PRIMARY KEY,
    operation_type TEXT NOT NULL,
    operator_id TEXT NOT NULL DEFAULT 'default_operator',
    chat_id TEXT NOT NULL,
    project_id TEXT REFERENCES projects(project_id),
    resource_key TEXT,
    request_key TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    base_version TEXT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending','confirmed','cancelled','expired','superseded','failed')
    ),
    phase TEXT NOT NULL DEFAULT 'queued' CHECK (
        phase IN ('queued','evaluating','needs_review','committed','no_change','superseded','failed')
    ),
    checkpoint_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT NOT NULL DEFAULT '{}',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    retry_at TEXT,
    lease_owner TEXT,
    lease_expires_at TEXT,
    updated_at TEXT,
    trigger_kind TEXT NOT NULL DEFAULT 'operator',
    requested_commit INTEGER NOT NULL DEFAULT 0 CHECK (requested_commit IN (0,1)),
    create_if_missing INTEGER NOT NULL DEFAULT 0 CHECK (create_if_missing IN (0,1)),
    trusted_surface TEXT,
    proposed_update_id TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    resolved_at TEXT,
    resolution TEXT
);
CREATE INDEX IF NOT EXISTS idx_pending_operations_scope
    ON pending_operations(operator_id, chat_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_pending_operations_project
    ON pending_operations(project_id, operation_type, status);
CREATE INDEX IF NOT EXISTS idx_pending_operations_expiry
    ON pending_operations(expires_at, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_operations_request_key
    ON pending_operations(request_key) WHERE request_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_pending_operations_resource_phase
    ON pending_operations(resource_key, operation_type, phase, updated_at DESC);

-- Digest notification queue (batches non-urgent notifications for twice-daily delivery)
CREATE TABLE IF NOT EXISTS digest_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL CHECK (category IN (
        'sla_alert','followup_overdue','ticket_aging',
        'email_ambiguous','email_deferred','support_ticket'
    )),
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    project_id TEXT,
    metadata_json TEXT DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    delivered_at TEXT,
    digest_batch_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_digest_queue_pending
    ON digest_queue(delivered_at, category);

-- Observation-bound durable learning queue
CREATE TABLE IF NOT EXISTS learning_outbox (
    job_id TEXT PRIMARY KEY,
    observation_id TEXT NOT NULL REFERENCES learning_observations(observation_id),
    job_type TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued'
        CHECK (state IN ('queued','processing','retry','completed','quarantined','dead_letter')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    available_at TEXT NOT NULL DEFAULT (datetime('now')),
    lease_token TEXT,
    leased_at TEXT,
    lease_expires_at TEXT,
    last_error TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT,
    effect_key TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT,
    UNIQUE(observation_id, job_type, extractor_version)
);
CREATE INDEX IF NOT EXISTS idx_learning_outbox_due
    ON learning_outbox(state, available_at, lease_expires_at);
CREATE INDEX IF NOT EXISTS idx_learning_outbox_observation
    ON learning_outbox(observation_id, job_type, extractor_version);

-- Exactly-once downstream effect ledger. Reservation and DB materialization share one transaction.
CREATE TABLE IF NOT EXISTS learning_job_effects (
    effect_key TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES learning_outbox(job_id),
    observation_id TEXT NOT NULL REFERENCES learning_observations(observation_id),
    effect_type TEXT NOT NULL,
    result_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_learning_job_effects_job
    ON learning_job_effects(job_id, observation_id);

-- Versioned candidate proposals and deterministic governance lifecycle.
CREATE TABLE IF NOT EXISTS memory_candidates (
    candidate_id TEXT NOT NULL,
    observation_id TEXT NOT NULL REFERENCES learning_observations(observation_id),
    candidate_version INTEGER NOT NULL CHECK (candidate_version >= 1),
    target_type TEXT NOT NULL CHECK (target_type IN ('semantic_fact','preference','procedure','wiki_publication')),
    subject_type TEXT NOT NULL,
    subject_id TEXT,
    predicate TEXT NOT NULL,
    proposed_value_json TEXT NOT NULL,
    scope_type TEXT NOT NULL CHECK (scope_type IN ('contact','project','counterparty','operator','market','organization','global')),
    scope_id TEXT,
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    suggested_expiry TEXT,
    reasoning_summary TEXT NOT NULL,
    risk_signals_json TEXT NOT NULL DEFAULT '[]',
    evidence_refs_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'proposed' CHECK (status = 'proposed'),
    extractor_version TEXT NOT NULL,
    model_version TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    revision_of_validation_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY(candidate_id, candidate_version),
    UNIQUE(candidate_id, candidate_version)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_candidates_observation_extractor
    ON memory_candidates(observation_id, target_type, extractor_version, candidate_version);
CREATE INDEX IF NOT EXISTS idx_memory_candidates_scope
    ON memory_candidates(scope_type, scope_id, target_type, created_at DESC);
CREATE TRIGGER IF NOT EXISTS trg_memory_candidates_content_immutable
BEFORE UPDATE ON memory_candidates
WHEN NEW.candidate_id IS NOT OLD.candidate_id
  OR NEW.observation_id IS NOT OLD.observation_id
  OR NEW.candidate_version IS NOT OLD.candidate_version
  OR NEW.target_type IS NOT OLD.target_type
  OR NEW.subject_type IS NOT OLD.subject_type
  OR NEW.subject_id IS NOT OLD.subject_id
  OR NEW.predicate IS NOT OLD.predicate
  OR NEW.proposed_value_json IS NOT OLD.proposed_value_json
  OR NEW.scope_type IS NOT OLD.scope_type
  OR NEW.scope_id IS NOT OLD.scope_id
  OR NEW.confidence IS NOT OLD.confidence
  OR NEW.suggested_expiry IS NOT OLD.suggested_expiry
  OR NEW.reasoning_summary IS NOT OLD.reasoning_summary
  OR NEW.risk_signals_json IS NOT OLD.risk_signals_json
  OR NEW.evidence_refs_json IS NOT OLD.evidence_refs_json
  OR NEW.extractor_version IS NOT OLD.extractor_version
  OR NEW.model_version IS NOT OLD.model_version
  OR NEW.prompt_version IS NOT OLD.prompt_version
  OR NEW.schema_version IS NOT OLD.schema_version
  OR NEW.revision_of_validation_id IS NOT OLD.revision_of_validation_id
  OR NEW.created_at IS NOT OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'memory candidate content is immutable');
END;

CREATE TABLE IF NOT EXISTS candidate_validation_runs (
    validation_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    candidate_version INTEGER NOT NULL,
    validator_version TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    policy_json TEXT NOT NULL,
    passed INTEGER NOT NULL CHECK (passed IN (0,1)),
    findings_json TEXT NOT NULL,
    risk_level TEXT NOT NULL CHECK (risk_level IN ('low','medium','high','critical')),
    risk_score INTEGER NOT NULL CHECK (risk_score >= 0 AND risk_score <= 100),
    input_hash TEXT NOT NULL,
    input_snapshot_json TEXT NOT NULL,
    findings_hash TEXT NOT NULL,
    disposition TEXT NOT NULL CHECK (disposition IN (
        'needs_revision','needs_review','rejected','quarantined'
    )),
    requires_review INTEGER NOT NULL CHECK (requires_review IN (0,1)),
    requires_evaluation INTEGER NOT NULL CHECK (requires_evaluation IN (0,1)),
    validation_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY(candidate_id, candidate_version)
        REFERENCES memory_candidates(candidate_id, candidate_version)
);
CREATE TABLE IF NOT EXISTS candidate_governance_states (
    candidate_id TEXT NOT NULL,
    candidate_version INTEGER NOT NULL,
    disposition TEXT NOT NULL CHECK (disposition IN (
        'needs_revision','needs_review','rejected','quarantined'
    )),
    validation_id TEXT NOT NULL REFERENCES candidate_validation_runs(validation_id),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(candidate_id, candidate_version),
    FOREIGN KEY(candidate_id, candidate_version)
        REFERENCES memory_candidates(candidate_id, candidate_version)
);
CREATE INDEX IF NOT EXISTS idx_candidate_governance_disposition
    ON candidate_governance_states(disposition, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_candidate_validation_candidate
    ON candidate_validation_runs(candidate_id, candidate_version, created_at DESC);
CREATE TRIGGER IF NOT EXISTS trg_candidate_validation_no_update
BEFORE UPDATE ON candidate_validation_runs
BEGIN
    SELECT RAISE(ABORT, 'candidate_validation_runs is append-only');
END;
CREATE TRIGGER IF NOT EXISTS trg_candidate_validation_no_delete
BEFORE DELETE ON candidate_validation_runs
BEGIN
    SELECT RAISE(ABORT, 'candidate_validation_runs is append-only');
END;

CREATE TABLE IF NOT EXISTS candidate_semantic_keys (
    candidate_id TEXT NOT NULL,
    candidate_version INTEGER NOT NULL,
    canonical_key TEXT NOT NULL,
    value_fingerprint TEXT NOT NULL,
    canonical_candidate_id TEXT NOT NULL,
    canonical_candidate_version INTEGER NOT NULL,
    match_type TEXT NOT NULL DEFAULT 'unique' CHECK (match_type IN ('unique','distinct','exact_duplicate','near_duplicate','contradiction')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY(candidate_id, candidate_version),
    FOREIGN KEY(candidate_id, candidate_version) REFERENCES memory_candidates(candidate_id, candidate_version),
    FOREIGN KEY(canonical_candidate_id, canonical_candidate_version) REFERENCES memory_candidates(candidate_id, candidate_version)
);
CREATE INDEX IF NOT EXISTS idx_candidate_semantic_canonical ON candidate_semantic_keys(canonical_key, created_at);
CREATE TABLE IF NOT EXISTS candidate_evidence (
    candidate_id TEXT NOT NULL,
    candidate_version INTEGER NOT NULL,
    observation_id TEXT NOT NULL REFERENCES learning_observations(observation_id),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY(candidate_id, candidate_version, observation_id),
    FOREIGN KEY(candidate_id, candidate_version) REFERENCES memory_candidates(candidate_id, candidate_version)
);
CREATE TABLE IF NOT EXISTS memory_conflicts (
    conflict_id TEXT PRIMARY KEY,
    canonical_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('unresolved','auto-resolved','review-resolved','superseded')),
    risk_level TEXT NOT NULL CHECK (risk_level IN ('low','medium','high','critical')),
    authority_blocked INTEGER NOT NULL DEFAULT 0 CHECK (authority_blocked IN (0,1)),
    superseded_by_candidate_id TEXT,
    superseded_by_version INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY(superseded_by_candidate_id, superseded_by_version) REFERENCES memory_candidates(candidate_id, candidate_version)
);
CREATE TABLE IF NOT EXISTS memory_conflict_members (
    conflict_id TEXT NOT NULL REFERENCES memory_conflicts(conflict_id),
    candidate_id TEXT NOT NULL,
    candidate_version INTEGER NOT NULL,
    PRIMARY KEY(conflict_id, candidate_id, candidate_version),
    FOREIGN KEY(candidate_id, candidate_version) REFERENCES memory_candidates(candidate_id, candidate_version)
);
CREATE TABLE IF NOT EXISTS memory_conflict_relations (
    relation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    conflict_id TEXT NOT NULL REFERENCES memory_conflicts(conflict_id),
    left_candidate_id TEXT NOT NULL,
    left_candidate_version INTEGER NOT NULL,
    right_candidate_id TEXT NOT NULL,
    right_candidate_version INTEGER NOT NULL,
    relation_type TEXT NOT NULL CHECK (relation_type IN ('equivalent','opposes','supersedes')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(conflict_id, left_candidate_id, left_candidate_version, right_candidate_id, right_candidate_version, relation_type),
    FOREIGN KEY(left_candidate_id, left_candidate_version) REFERENCES memory_candidates(candidate_id, candidate_version),
    FOREIGN KEY(right_candidate_id, right_candidate_version) REFERENCES memory_candidates(candidate_id, candidate_version)
);

CREATE TABLE IF NOT EXISTS memory_conflict_events (
    conflict_id TEXT NOT NULL REFERENCES memory_conflicts(conflict_id),
    event_seq INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('unresolved','auto-resolved','review-resolved','superseded')),
    risk_level TEXT NOT NULL CHECK (risk_level IN ('low','medium','high','critical')),
    actor_id TEXT NOT NULL,
    superseded_by_candidate_id TEXT,
    superseded_by_version INTEGER,
    event_type TEXT NOT NULL DEFAULT 'status-changed' CHECK (event_type IN ('status-changed','risk-escalated')),
    reason TEXT,
    action TEXT,
    confirmation_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY(conflict_id, event_seq),
    FOREIGN KEY(superseded_by_candidate_id, superseded_by_version) REFERENCES memory_candidates(candidate_id, candidate_version)
);
CREATE TRIGGER IF NOT EXISTS trg_memory_conflict_events_no_update BEFORE UPDATE ON memory_conflict_events BEGIN SELECT RAISE(ABORT, 'memory_conflict_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_memory_conflict_events_no_delete BEFORE DELETE ON memory_conflict_events BEGIN SELECT RAISE(ABORT, 'memory_conflict_events is append-only'); END;
CREATE INDEX IF NOT EXISTS idx_memory_conflicts_key_status ON memory_conflicts(canonical_key, status, risk_level);
CREATE INDEX IF NOT EXISTS idx_memory_conflict_members_candidate ON memory_conflict_members(candidate_id, candidate_version);
CREATE TABLE IF NOT EXISTS candidate_merge_decisions (
    decision_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    canonical_candidate_id TEXT NOT NULL,
    canonical_candidate_version INTEGER NOT NULL,
    merged_candidate_id TEXT NOT NULL,
    merged_candidate_version INTEGER NOT NULL,
    conflict_id TEXT REFERENCES memory_conflicts(conflict_id),
    actor_id TEXT NOT NULL CHECK(length(trim(actor_id)) > 0),
    reason TEXT NOT NULL CHECK(length(trim(reason)) > 0),
    created_at TEXT NOT NULL,
    FOREIGN KEY(canonical_candidate_id, canonical_candidate_version) REFERENCES memory_candidates(candidate_id, candidate_version),
    FOREIGN KEY(merged_candidate_id, merged_candidate_version) REFERENCES memory_candidates(candidate_id, candidate_version)
);
CREATE TRIGGER IF NOT EXISTS trg_candidate_merge_decisions_no_update BEFORE UPDATE ON candidate_merge_decisions BEGIN SELECT RAISE(ABORT, 'candidate_merge_decisions is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_candidate_merge_decisions_no_delete BEFORE DELETE ON candidate_merge_decisions BEGIN SELECT RAISE(ABORT, 'candidate_merge_decisions is append-only'); END;


CREATE TABLE IF NOT EXISTS candidate_evaluations (
 evaluation_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL, candidate_version INTEGER NOT NULL, runner_version TEXT NOT NULL, policy_version TEXT NOT NULL, suite_manifest_json TEXT NOT NULL, baseline_outputs_json TEXT NOT NULL, candidate_outputs_json TEXT NOT NULL, scores_json TEXT NOT NULL, safety_assertions_json TEXT NOT NULL, failure_codes_json TEXT NOT NULL, input_hash TEXT NOT NULL, output_hash TEXT NOT NULL, suite_hash TEXT NOT NULL, config_hash TEXT NOT NULL, replay_hash TEXT NOT NULL, runtime_hash TEXT NOT NULL, passed INTEGER NOT NULL CHECK (passed IN (0,1)), evaluated_at TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT (datetime('now')), FOREIGN KEY(candidate_id,candidate_version) REFERENCES memory_candidates(candidate_id,candidate_version));
CREATE INDEX IF NOT EXISTS idx_candidate_evaluations_candidate ON candidate_evaluations(candidate_id,candidate_version,evaluated_at DESC);
CREATE TRIGGER IF NOT EXISTS trg_candidate_evaluations_no_update BEFORE UPDATE ON candidate_evaluations BEGIN SELECT RAISE(ABORT, 'candidate_evaluations is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_candidate_evaluations_no_delete BEFORE DELETE ON candidate_evaluations BEGIN SELECT RAISE(ABORT, 'candidate_evaluations is append-only'); END;


-- Learning governance decisions and materialized facts (v73)
CREATE TABLE IF NOT EXISTS memory_decisions (
 decision_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL, candidate_version INTEGER NOT NULL,
 decision_type TEXT NOT NULL CHECK(decision_type IN ('approve','reject','edit_and_approve','narrow_scope','quarantine','supersede','revoke','expire','revalidate')),
 actor_id TEXT NOT NULL CHECK(length(trim(actor_id))>0), reason TEXT NOT NULL CHECK(length(trim(reason))>0),
 validation_id TEXT NOT NULL REFERENCES candidate_validation_runs(validation_id), evaluation_id TEXT REFERENCES candidate_evaluations(evaluation_id),
 replaces_fact_id TEXT, decision_payload_json TEXT NOT NULL DEFAULT '{}', decided_at TEXT NOT NULL,
 FOREIGN KEY(candidate_id,candidate_version) REFERENCES memory_candidates(candidate_id,candidate_version));
CREATE TRIGGER IF NOT EXISTS trg_memory_decisions_no_update BEFORE UPDATE ON memory_decisions BEGIN SELECT RAISE(ABORT,'memory_decisions is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_memory_decisions_no_delete BEFORE DELETE ON memory_decisions BEGIN SELECT RAISE(ABORT,'memory_decisions is append-only'); END;
CREATE TABLE IF NOT EXISTS memory_facts (
 fact_id TEXT PRIMARY KEY, fact_key TEXT NOT NULL, fact_version INTEGER NOT NULL CHECK(fact_version>=1), candidate_id TEXT NOT NULL, candidate_version INTEGER NOT NULL,
 validation_id TEXT NOT NULL REFERENCES candidate_validation_runs(validation_id), decision_id TEXT NOT NULL REFERENCES memory_decisions(decision_id), evaluation_id TEXT REFERENCES candidate_evaluations(evaluation_id),
 target_type TEXT NOT NULL, subject_type TEXT NOT NULL, subject_id TEXT, predicate TEXT NOT NULL, value_json TEXT NOT NULL, scope_type TEXT NOT NULL, scope_id TEXT,
 evidence_refs_json TEXT NOT NULL, provenance_json TEXT NOT NULL, valid_from TEXT NOT NULL, valid_to TEXT, status TEXT NOT NULL CHECK(status IN ('active','superseded','revoked')),
 UNIQUE(fact_key,fact_version), FOREIGN KEY(candidate_id,candidate_version) REFERENCES memory_candidates(candidate_id,candidate_version));
CREATE TRIGGER IF NOT EXISTS trg_active_fact_approved BEFORE INSERT ON memory_facts WHEN NEW.status='active' AND NOT EXISTS (SELECT 1 FROM memory_decisions d WHERE d.decision_id=NEW.decision_id AND d.candidate_id=NEW.candidate_id AND d.candidate_version=NEW.candidate_version AND d.decision_type IN ('approve','edit_and_approve','narrow_scope','supersede')) BEGIN SELECT RAISE(ABORT,'active fact requires approved decision'); END;
CREATE TRIGGER IF NOT EXISTS trg_memory_facts_content_immutable BEFORE UPDATE ON memory_facts WHEN NEW.fact_id!=OLD.fact_id OR NEW.fact_key!=OLD.fact_key OR NEW.fact_version!=OLD.fact_version OR NEW.candidate_id!=OLD.candidate_id OR NEW.candidate_version!=OLD.candidate_version OR NEW.validation_id!=OLD.validation_id OR NEW.decision_id!=OLD.decision_id OR NEW.value_json!=OLD.value_json OR NEW.evidence_refs_json!=OLD.evidence_refs_json OR NEW.provenance_json!=OLD.provenance_json OR NEW.valid_from!=OLD.valid_from BEGIN SELECT RAISE(ABORT,'memory_facts content is immutable'); END;
CREATE TABLE IF NOT EXISTS memory_projection_outbox (event_id TEXT PRIMARY KEY,fact_id TEXT NOT NULL REFERENCES memory_facts(fact_id),event_type TEXT NOT NULL CHECK(event_type IN ('upsert','delete')),status TEXT NOT NULL CHECK(status IN ('pending','completed','partial_failed','dead_letter')) DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,last_error TEXT,created_at TEXT NOT NULL DEFAULT(datetime('now')),completed_at TEXT);
CREATE TABLE IF NOT EXISTS memory_audit_events (event_id TEXT PRIMARY KEY,event_type TEXT NOT NULL,decision_id TEXT NOT NULL REFERENCES memory_decisions(decision_id),fact_id TEXT,payload_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS trg_memory_audit_no_update BEFORE UPDATE ON memory_audit_events BEGIN SELECT RAISE(ABORT,'memory_audit_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_memory_audit_no_delete BEFORE DELETE ON memory_audit_events BEGIN SELECT RAISE(ABORT,'memory_audit_events is append-only'); END;

-- WP-08 hardening state and projection ownership (v74)
CREATE TABLE IF NOT EXISTS memory_fact_heads (fact_key TEXT PRIMARY KEY, fact_id TEXT NOT NULL UNIQUE REFERENCES memory_facts(fact_id), fact_version INTEGER NOT NULL);
CREATE TRIGGER IF NOT EXISTS trg_memory_fact_heads_binding BEFORE INSERT ON memory_fact_heads WHEN NOT EXISTS (SELECT 1 FROM memory_facts f WHERE f.fact_id=NEW.fact_id AND f.fact_key=NEW.fact_key AND f.fact_version=NEW.fact_version) OR NOT EXISTS (SELECT 1 FROM memory_fact_state_events e WHERE e.fact_id=NEW.fact_id AND e.state='active' AND e.event_id=(SELECT event_id FROM memory_fact_state_events WHERE fact_id=NEW.fact_id ORDER BY effective_at DESC,created_at DESC,event_seq DESC LIMIT 1)) BEGIN SELECT RAISE(ABORT,'memory_fact_heads binding invalid'); END;
CREATE TRIGGER IF NOT EXISTS trg_memory_fact_heads_no_update BEFORE UPDATE ON memory_fact_heads BEGIN SELECT RAISE(ABORT,'memory_fact_heads is derived'); END;
CREATE TABLE IF NOT EXISTS memory_fact_state_events (event_seq INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT NOT NULL UNIQUE, fact_id TEXT NOT NULL REFERENCES memory_facts(fact_id), state TEXT NOT NULL CHECK(state IN ('active','superseded','revoked')), actor_id TEXT NOT NULL, reason TEXT NOT NULL, effective_at TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT(datetime('now')));
CREATE TRIGGER IF NOT EXISTS trg_memory_fact_state_no_update BEFORE UPDATE ON memory_fact_state_events BEGIN SELECT RAISE(ABORT,'memory_fact_state_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_memory_fact_state_no_delete BEFORE DELETE ON memory_fact_state_events BEGIN SELECT RAISE(ABORT,'memory_fact_state_events is append-only'); END;
CREATE TABLE IF NOT EXISTS memory_projection_deliveries (event_id TEXT NOT NULL REFERENCES memory_projection_outbox(event_id), target TEXT NOT NULL CHECK(target IN ('learned_rules','response_pattern','operator_memory')), status TEXT NOT NULL CHECK(status IN ('pending','completed','dead_letter')) DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, completed_at TEXT, PRIMARY KEY(event_id,target));
CREATE TABLE IF NOT EXISTS memory_projection_ownership (target TEXT NOT NULL, legacy_key TEXT NOT NULL, fact_id TEXT NOT NULL REFERENCES memory_facts(fact_id), fact_version INTEGER NOT NULL, snapshot_json TEXT, PRIMARY KEY(target,legacy_key));
CREATE TRIGGER IF NOT EXISTS trg_memory_facts_no_update BEFORE UPDATE ON memory_facts BEGIN SELECT RAISE(ABORT,'memory_facts is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_memory_facts_no_delete BEFORE DELETE ON memory_facts BEGIN SELECT RAISE(ABORT,'memory_facts is append-only'); END;

-- WP-11 deterministic lifecycle, review, governed validity, and rollback operations (v84)

CREATE TABLE IF NOT EXISTS memory_lifecycle_policies (
 policy_version TEXT NOT NULL,target_type TEXT NOT NULL CHECK(target_type IN ('semantic_fact','preference','procedure','wiki_publication')),
 default_ttl_seconds INTEGER NOT NULL CHECK(default_ttl_seconds>0),review_interval_seconds INTEGER NOT NULL CHECK(review_interval_seconds>0),
 revalidation_window_seconds INTEGER NOT NULL CHECK(revalidation_window_seconds>0),max_revalidation_horizon_seconds INTEGER CHECK(max_revalidation_horizon_seconds>0),
 expiry_required INTEGER NOT NULL CHECK(expiry_required IN (0,1)),negative_review_threshold INTEGER NOT NULL CHECK(negative_review_threshold>0),
 high_risk_behavior TEXT NOT NULL CHECK(high_risk_behavior IN ('quarantine_pending_review','remain_active_with_warning')),
 effective_from TEXT NOT NULL,effective_to TEXT,policy_hash TEXT NOT NULL,PRIMARY KEY(policy_version,target_type));
CREATE TABLE IF NOT EXISTS memory_fact_lifecycle_policy (
 fact_id TEXT PRIMARY KEY REFERENCES memory_facts(fact_id),policy_version TEXT NOT NULL,target_type TEXT NOT NULL,
 effective_from TEXT NOT NULL,effective_to TEXT,approved_valid_to TEXT NOT NULL,
 FOREIGN KEY(policy_version,target_type) REFERENCES memory_lifecycle_policies(policy_version,target_type));
CREATE TABLE IF NOT EXISTS memory_fact_validity_events (
 event_id TEXT PRIMARY KEY,fact_id TEXT NOT NULL REFERENCES memory_facts(fact_id),event_type TEXT NOT NULL CHECK(event_type IN ('approved','revalidated')),
 valid_to TEXT NOT NULL,next_review_at TEXT NOT NULL,actor_id TEXT NOT NULL,actor_role TEXT NOT NULL,reason TEXT NOT NULL,
 source_outcome_ids_json TEXT NOT NULL DEFAULT '[]',effective_at TEXT NOT NULL,idempotency_key TEXT NOT NULL UNIQUE,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memory_lifecycle_events (
 event_id TEXT PRIMARY KEY,fact_id TEXT NOT NULL REFERENCES memory_facts(fact_id),
 event_type TEXT NOT NULL CHECK(event_type IN ('expired','review_required','revalidated','review_resolved','rollback_requested')),
 reason_code TEXT NOT NULL,source_outcome_id TEXT,effective_at TEXT NOT NULL,idempotency_key TEXT NOT NULL UNIQUE,
 payload_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memory_lifecycle_reviews (
 review_id TEXT PRIMARY KEY,fact_id TEXT NOT NULL REFERENCES memory_facts(fact_id),fact_version INTEGER NOT NULL,
 generation INTEGER NOT NULL CHECK(generation>0),reason_code TEXT NOT NULL,evidence_hash TEXT NOT NULL,evidence_json TEXT NOT NULL,opened_at TEXT NOT NULL,
 UNIQUE(fact_id,fact_version,reason_code,evidence_hash));
CREATE TABLE IF NOT EXISTS memory_lifecycle_review_events (
 event_id TEXT PRIMARY KEY,review_id TEXT NOT NULL REFERENCES memory_lifecycle_reviews(review_id),
 status TEXT NOT NULL CHECK(status IN ('pending','approved','revalidated','quarantined','revoked','superseded')),
 actor_id TEXT NOT NULL,reason TEXT NOT NULL,effective_at TEXT NOT NULL,idempotency_key TEXT NOT NULL UNIQUE,
 payload_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memory_lifecycle_review_evidence (
 event_id TEXT PRIMARY KEY,review_id TEXT NOT NULL REFERENCES memory_lifecycle_reviews(review_id),
 outcome_id TEXT NOT NULL,effective_at TEXT NOT NULL,idempotency_key TEXT NOT NULL UNIQUE,created_at TEXT NOT NULL,
 UNIQUE(review_id,outcome_id));
CREATE TABLE IF NOT EXISTS memory_authority_legacy_quarantine (
 quarantine_id TEXT PRIMARY KEY,selector_kind TEXT NOT NULL CHECK(selector_kind='legacy_source_type'),selector_value TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status='quarantined'),operation_id TEXT,actor_id TEXT NOT NULL,actor_role TEXT NOT NULL,reason TEXT NOT NULL,
 effective_at TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memory_authority_denylist (
 deny_id TEXT PRIMARY KEY,authority_kind TEXT NOT NULL CHECK(authority_kind IN ('source_id','content_hash','batch_id')),
 authority_value TEXT NOT NULL,operation_id TEXT NOT NULL,actor_id TEXT NOT NULL,actor_role TEXT NOT NULL,
 reason TEXT NOT NULL,effective_at TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(authority_kind,authority_value));
CREATE TABLE IF NOT EXISTS memory_lifecycle_operations (
 operation_id TEXT PRIMARY KEY,operation_type TEXT NOT NULL CHECK(operation_type IN ('emergency_revoke','expiry','review_resolution','rollback','supersession')),
 idempotency_key TEXT NOT NULL UNIQUE,request_hash TEXT NOT NULL,actor_id TEXT NOT NULL,actor_role TEXT NOT NULL,
 reason TEXT NOT NULL,selector_kind TEXT,selector_value TEXT,started_at TEXT NOT NULL,effective_at TEXT NOT NULL,deadline TEXT NOT NULL,
 completed_at TEXT,status TEXT NOT NULL CHECK(status IN ('pending','completed','partial_failed','dead_letter','slo_breached')),
 result_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memory_lifecycle_operation_events (
 event_id TEXT PRIMARY KEY,operation_id TEXT NOT NULL REFERENCES memory_lifecycle_operations(operation_id),
 status TEXT NOT NULL CHECK(status IN ('pending','completed','partial_failed','dead_letter','slo_breached')),
 reason TEXT NOT NULL,effective_at TEXT NOT NULL,payload_json TEXT NOT NULL DEFAULT '{}',idempotency_key TEXT NOT NULL UNIQUE,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memory_rollback_targets (
 operation_id TEXT NOT NULL REFERENCES memory_lifecycle_operations(operation_id),fact_id TEXT NOT NULL REFERENCES memory_facts(fact_id),
 target TEXT NOT NULL CHECK(target IN ('compatibility_projection','wiki_authority','wiki_index','context_cache')),
 status TEXT NOT NULL CHECK(status IN ('pending','completed','partial_failed','dead_letter','slo_breached','manual_review')),
 attempts INTEGER NOT NULL DEFAULT 0,last_error TEXT,completed_at TEXT,projection_event_id TEXT,ownership_snapshot_json TEXT,
 PRIMARY KEY(operation_id,fact_id,target));
CREATE TABLE IF NOT EXISTS memory_rollback_attempts (
 attempt_id TEXT PRIMARY KEY,operation_id TEXT NOT NULL,fact_id TEXT NOT NULL,target TEXT NOT NULL,attempt_number INTEGER NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('completed','partial_failed','dead_letter')),error TEXT,started_at TEXT NOT NULL,completed_at TEXT NOT NULL,
 FOREIGN KEY(operation_id,fact_id,target) REFERENCES memory_rollback_targets(operation_id,fact_id,target),
 UNIQUE(operation_id,fact_id,target,attempt_number));
CREATE INDEX IF NOT EXISTS idx_memory_validity_fact ON memory_fact_validity_events(fact_id,effective_at);
CREATE INDEX IF NOT EXISTS idx_memory_lifecycle_events_fact ON memory_lifecycle_events(fact_id,effective_at);
CREATE INDEX IF NOT EXISTS idx_memory_review_events_review ON memory_lifecycle_review_events(review_id,effective_at);
CREATE INDEX IF NOT EXISTS idx_memory_rollback_status ON memory_rollback_targets(status,operation_id);

-- Governed Wiki publication v2 (v87)
CREATE TABLE IF NOT EXISTS wiki_publications (
 publication_id TEXT PRIMARY KEY,fact_id TEXT NOT NULL UNIQUE REFERENCES memory_facts(fact_id),fact_version INTEGER NOT NULL,
 decision_id TEXT NOT NULL REFERENCES memory_decisions(decision_id),canonical_path TEXT NOT NULL,
 operation TEXT NOT NULL CHECK(operation IN ('upsert','revoke')),
 status TEXT NOT NULL CHECK(status IN ('proposed','publishing','active','failed','manual_review','dead_letter','revoked')),
 planned_content_hash TEXT NOT NULL,page_hash TEXT,owned_content_hash TEXT,page_revision INTEGER NOT NULL DEFAULT 0,
 repair_count INTEGER NOT NULL DEFAULT 0 CHECK(repair_count BETWEEN 0 AND 2),attempts INTEGER NOT NULL DEFAULT 0,
 source_watermark TEXT,scope TEXT NOT NULL CHECK(scope IN ('global','project','market','counterparty')),
 visibility TEXT NOT NULL CHECK(visibility IN ('internal_only','external_safe','restricted')),
 project_ids_json TEXT NOT NULL DEFAULT '[]',markets_json TEXT NOT NULL DEFAULT '[]',counterparties_json TEXT NOT NULL DEFAULT '[]',
 ownership_json TEXT NOT NULL,last_error TEXT,created_at TEXT NOT NULL DEFAULT(datetime('now')),completed_at TEXT,revoked_at TEXT);
CREATE TABLE IF NOT EXISTS wiki_publication_revisions (
 revision_id TEXT PRIMARY KEY,publication_id TEXT NOT NULL REFERENCES wiki_publications(publication_id),revision INTEGER NOT NULL,
 repair_of_revision INTEGER,content_hash TEXT NOT NULL,page_hash TEXT NOT NULL,content TEXT NOT NULL,lint_json TEXT NOT NULL DEFAULT '[]',
 created_at TEXT NOT NULL DEFAULT(datetime('now')),UNIQUE(publication_id,revision));
CREATE TABLE IF NOT EXISTS wiki_publication_history (
 history_id TEXT PRIMARY KEY,publication_id TEXT NOT NULL REFERENCES wiki_publications(publication_id),event_type TEXT NOT NULL,
 actor_id TEXT NOT NULL,reason TEXT NOT NULL,payload_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL DEFAULT(datetime('now')));
CREATE TABLE IF NOT EXISTS wiki_publication_acknowledgements (
 source_watermark TEXT PRIMARY KEY,publication_id TEXT NOT NULL UNIQUE REFERENCES wiki_publications(publication_id),
 revision INTEGER NOT NULL,content_hash TEXT NOT NULL,page_hash TEXT NOT NULL,
 indexed_at TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT(datetime('now')));
CREATE INDEX IF NOT EXISTS idx_wiki_publications_status ON wiki_publications(status,created_at);
CREATE TRIGGER IF NOT EXISTS trg_wiki_publication_revisions_no_update BEFORE UPDATE ON wiki_publication_revisions BEGIN SELECT RAISE(ABORT,'wiki publication revisions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_wiki_publication_revisions_no_delete BEFORE DELETE ON wiki_publication_revisions BEGIN SELECT RAISE(ABORT,'wiki publication revisions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_wiki_publication_history_no_update BEFORE UPDATE ON wiki_publication_history BEGIN SELECT RAISE(ABORT,'wiki publication history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_wiki_publication_history_no_delete BEFORE DELETE ON wiki_publication_history BEGIN SELECT RAISE(ABORT,'wiki publication history is append-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_decision_binding BEFORE INSERT ON memory_decisions WHEN NOT EXISTS (SELECT 1 FROM candidate_validation_runs v WHERE v.validation_id=NEW.validation_id AND v.candidate_id=NEW.candidate_id AND v.candidate_version=NEW.candidate_version) OR (NEW.evaluation_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM candidate_evaluations e WHERE e.evaluation_id=NEW.evaluation_id AND e.candidate_id=NEW.candidate_id AND e.candidate_version=NEW.candidate_version AND e.passed=1)) BEGIN SELECT RAISE(ABORT,'decision references do not bind candidate version'); END;
CREATE TRIGGER IF NOT EXISTS trg_fact_binding BEFORE INSERT ON memory_facts WHEN NOT EXISTS (SELECT 1 FROM memory_decisions d WHERE d.decision_id=NEW.decision_id AND d.candidate_id=NEW.candidate_id AND d.candidate_version=NEW.candidate_version AND d.validation_id=NEW.validation_id AND d.evaluation_id IS NEW.evaluation_id) BEGIN SELECT RAISE(ABORT,'fact references do not bind decision'); END;

-- Learning rollout, shadow parity, and ingress receipts (v90)
CREATE TABLE IF NOT EXISTS learning_rollout_policies (
 policy_id TEXT PRIMARY KEY,surface TEXT NOT NULL,
 cohort_type TEXT NOT NULL CHECK(cohort_type IN ('surface','operator','project','operator_project')),
 cohort_id TEXT NOT NULL,operator_id TEXT,project_id TEXT,mode TEXT NOT NULL CHECK(mode IN ('legacy','shadow','v2')),
 version INTEGER NOT NULL CHECK(version>=1),actor TEXT NOT NULL,reason TEXT NOT NULL,updated_at TEXT NOT NULL,
 UNIQUE(surface,cohort_type,cohort_id));
CREATE TABLE IF NOT EXISTS learning_rollout_policy_events (
 event_id TEXT PRIMARY KEY,policy_id TEXT NOT NULL,surface TEXT NOT NULL,cohort_type TEXT NOT NULL,cohort_id TEXT NOT NULL,
 operator_id TEXT,project_id TEXT,old_mode TEXT,new_mode TEXT NOT NULL,policy_version INTEGER NOT NULL,
 actor TEXT NOT NULL,reason TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS trg_learning_rollout_events_no_update BEFORE UPDATE ON learning_rollout_policy_events BEGIN SELECT RAISE(ABORT,'rollout events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_learning_rollout_events_no_delete BEFORE DELETE ON learning_rollout_policy_events BEGIN SELECT RAISE(ABORT,'rollout events are append-only'); END;
CREATE TABLE IF NOT EXISTS learning_shadow_parity (
 telemetry_id TEXT PRIMARY KEY,surface TEXT NOT NULL,operator_id TEXT,project_id TEXT,cohort TEXT NOT NULL,policy_version INTEGER,
 trace_id TEXT,output_revision_id TEXT,legacy_hash TEXT NOT NULL,v2_hash TEXT NOT NULL,legacy_chars INTEGER NOT NULL,v2_chars INTEGER NOT NULL,
 changed_classification TEXT NOT NULL CHECK(changed_classification IN ('same','changed')),selected_facts_json TEXT NOT NULL DEFAULT '[]',
 error_category TEXT,created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_learning_shadow_parity_surface ON learning_shadow_parity(surface,created_at);
CREATE TABLE IF NOT EXISTS learning_shadow_parity_outbox (
 telemetry_id TEXT PRIMARY KEY,payload_json TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending'
   CHECK(status IN ('pending','completed','dead_letter')),
 attempts INTEGER NOT NULL DEFAULT 0,last_error TEXT,created_at TEXT NOT NULL,completed_at TEXT);
CREATE TABLE IF NOT EXISTS learning_ingress_receipts (
 event_id TEXT PRIMARY KEY,source_type TEXT NOT NULL,source_id TEXT NOT NULL,operator_id TEXT NOT NULL,project_id TEXT,
 dual_write_requested INTEGER NOT NULL,v2_status TEXT NOT NULL,legacy_status TEXT NOT NULL,status TEXT NOT NULL,
 v2_receipt_json TEXT,legacy_receipt_json TEXT,v2_error TEXT,legacy_error TEXT,
 v2_attempts INTEGER NOT NULL DEFAULT 0,legacy_attempts INTEGER NOT NULL DEFAULT 0,
 max_attempts INTEGER NOT NULL DEFAULT 3,updated_at TEXT NOT NULL,created_at TEXT NOT NULL,
 UNIQUE(source_type,source_id));

-- Legacy learning retirement boundary (v91)
CREATE TABLE IF NOT EXISTS learning_legacy_retirement (
 singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
 mode TEXT NOT NULL CHECK(mode IN ('compatibility','read_only','retired')),
 version INTEGER NOT NULL CHECK(version>=1),actor TEXT NOT NULL,reason TEXT NOT NULL,updated_at TEXT NOT NULL);
INSERT OR IGNORE INTO learning_legacy_retirement
 (singleton_id,mode,version,actor,reason,updated_at)
 VALUES (1,'compatibility',1,'migration','initial compatibility boundary',datetime('now'));
CREATE TABLE IF NOT EXISTS learning_legacy_retirement_gates (
 gate_name TEXT PRIMARY KEY CHECK(gate_name IN ('backfill_complete','parity_acceptable','canary_stable','stable_cycle_recorded')),
 passed INTEGER NOT NULL CHECK(passed IN (0,1)),evidence_ref TEXT NOT NULL,actor TEXT NOT NULL,reason TEXT NOT NULL,recorded_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS learning_legacy_retirement_events (
 event_id TEXT PRIMARY KEY,old_mode TEXT NOT NULL,new_mode TEXT NOT NULL,state_version INTEGER NOT NULL,
 actor TEXT NOT NULL,reason TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS trg_legacy_retirement_events_no_update BEFORE UPDATE ON learning_legacy_retirement_events BEGIN SELECT RAISE(ABORT,'retirement events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_legacy_retirement_events_no_delete BEFORE DELETE ON learning_legacy_retirement_events BEGIN SELECT RAISE(ABORT,'retirement events are append-only'); END;


-- Email Project Truth B privacy-safe shadow observations (v105)
CREATE TABLE IF NOT EXISTS email_project_truth_shadow (
    observation_id TEXT PRIMARY KEY,
    policy_version TEXT NOT NULL,
    project_id TEXT NOT NULL,
    source_ref_hash TEXT NOT NULL,
    parent_trace_id TEXT,
    parent_run_id TEXT,
    shadow_trace_id TEXT,
    shadow_run_id TEXT,
    baseline_status TEXT NOT NULL,
    candidate_status TEXT NOT NULL,
    baseline_field_hashes_json TEXT NOT NULL DEFAULT '{}',
    candidate_field_hashes_json TEXT NOT NULL DEFAULT '{}',
    comparison_classification TEXT NOT NULL,
    citation_coverage REAL NOT NULL DEFAULT 0 CHECK (citation_coverage >= 0 AND citation_coverage <= 1),
    unsafe_update INTEGER NOT NULL DEFAULT 0 CHECK (unsafe_update IN (0,1)),
    review_required INTEGER NOT NULL DEFAULT 0 CHECK (review_required IN (0,1)),
    review_reason_count INTEGER NOT NULL DEFAULT 0 CHECK (review_reason_count >= 0),
    accepted_citations INTEGER NOT NULL DEFAULT 0 CHECK (accepted_citations >= 0),
    total_citations INTEGER NOT NULL DEFAULT 0 CHECK (total_citations >= 0),
    rejected_citations INTEGER NOT NULL DEFAULT 0 CHECK (rejected_citations >= 0),
    model TEXT,
    latency_ms INTEGER NOT NULL DEFAULT 0 CHECK (latency_ms >= 0),
    queue_lag_ms REAL NOT NULL DEFAULT 0 CHECK (queue_lag_ms >= 0),
    error_category TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_email_project_truth_shadow_created
    ON email_project_truth_shadow(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_email_project_truth_shadow_project
    ON email_project_truth_shadow(project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_email_project_truth_shadow_parent_run
    ON email_project_truth_shadow(parent_run_id);
CREATE INDEX IF NOT EXISTS idx_email_project_truth_shadow_classification
    ON email_project_truth_shadow(comparison_classification, created_at DESC);
CREATE TRIGGER IF NOT EXISTS trg_email_project_truth_shadow_no_update
BEFORE UPDATE ON email_project_truth_shadow
BEGIN
    SELECT RAISE(ABORT, 'email_project_truth_shadow is append-only');
END;
CREATE TRIGGER IF NOT EXISTS trg_email_project_truth_shadow_no_delete
BEFORE DELETE ON email_project_truth_shadow
BEGIN
    SELECT RAISE(ABORT, 'email_project_truth_shadow is append-only');
END;

-- Internal actions (v39) — tasks assigned to internal teams with deadlines
CREATE TABLE IF NOT EXISTS internal_actions (
    action_id    TEXT PRIMARY KEY,
    project_id   TEXT REFERENCES projects(project_id),
    owner_team   TEXT NOT NULL CHECK (owner_team IN (
                     'IT Network', 'Compliance', 'Demo Provisioning Support',
                     'Demo Connectivity Acceptance Support', 'Demo IT Security',
                     'Demo Trading Ops', 'Demo Post-Trade')),
    title        TEXT NOT NULL,
    deadline     TEXT,
    status       TEXT NOT NULL DEFAULT 'open'
                     CHECK (status IN ('open', 'done', 'cancelled')),
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_internal_actions_project
    ON internal_actions(project_id, status);
"""


EMAIL_UPDATE_NOTIFICATION_OUTBOX_DDL = """
CREATE TABLE IF NOT EXISTS email_update_notification_outbox (
    update_id TEXT PRIMARY KEY REFERENCES proposed_updates(update_id),
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    payload_json TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','sending','sent','uncertain','cancelled')),
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TEXT NOT NULL,
    lease_token TEXT,
    lease_until TEXT,
    telegram_message_id TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_email_update_notification_pending
    ON email_update_notification_outbox(state, available_at);
"""


GO_LIVE_TELEGRAM_OUTBOX_DDL = """
CREATE TABLE IF NOT EXISTS go_live_telegram_outbox (
    project_id TEXT PRIMARY KEY REFERENCES projects(project_id),
    chat_id INTEGER NOT NULL,
    bot_fingerprint TEXT NOT NULL,
    text TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK(state IN ('pending','sending','sent','uncertain','cancelled')),
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TEXT NOT NULL,
    lease_token TEXT,
    lease_until TEXT,
    telegram_message_id TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_go_live_telegram_outbox_due
    ON go_live_telegram_outbox(state, available_at);
"""


SUPPORT_EMAIL_EFFECTS_DDL = """
CREATE TABLE IF NOT EXISTS support_email_effects (
    email_id TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL REFERENCES support_tickets(ticket_id),
    route_source_type TEXT NOT NULL DEFAULT 'unknown'
        CHECK(route_source_type IN ('imap','graph','direct','unknown')),
    notification_json TEXT,
    is_followup INTEGER NOT NULL,
    chat_id INTEGER,
    bot_fingerprint TEXT,
    card_state TEXT NOT NULL CHECK(card_state IN ('none','pending','sending','sent','uncertain')),
    draft_state TEXT NOT NULL CHECK(draft_state IN ('none','pending','scheduled')),
    draft_lease_token TEXT,
    draft_lease_until TEXT,
    draft_available_at TEXT,
    draft_last_error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TEXT NOT NULL,
    lease_token TEXT,
    lease_until TEXT,
    telegram_message_id TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_support_email_effects_due
    ON support_email_effects(card_state, available_at);
"""


from src.db.followup_plan_schema import DDL as FOLLOWUP_PLAN_DDL
from src.db.pct_completion_schema import DDL as PCT_COMPLETION_DDL
from src.db.followup_review_schema import DDL as FOLLOWUP_REVIEW_DDL
from src.db.go_live_schema import DDL as GO_LIVE_DDL
from src.db.inbound_work_schema import DDL as INBOUND_WORK_DDL
from src.db.commitment_card_schema import DDL as COMMITMENT_CARD_DDL
from src.db.inbound_overflow_schema import DDL as INBOUND_OVERFLOW_DDL
from src.db.inbound_presentation_schema import DDL as INBOUND_PRESENTATION_DDL
from src.db.contact_identity_card_schema import DDL as CONTACT_IDENTITY_CARD_DDL
from src.db.project_advisory_schema import DDL as PROJECT_ADVISORY_DDL
from src.db.draft_card_schema import DDL as DRAFT_CARD_DDL
from src.db.staged_update_card_schema import DDL as STAGED_UPDATE_CARD_DDL
from src.db.personal_reminder_schema import DDL as PERSONAL_REMINDER_DDL
from src.db.draft_attachment_schema import DDL as DRAFT_ATTACHMENT_DDL
SCHEMA_DDL += EMAIL_UPDATE_NOTIFICATION_OUTBOX_DDL + GO_LIVE_TELEGRAM_OUTBOX_DDL + SUPPORT_EMAIL_EFFECTS_DDL + FOLLOWUP_PLAN_DDL + PCT_COMPLETION_DDL + FOLLOWUP_REVIEW_DDL + GO_LIVE_DDL + INBOUND_WORK_DDL + COMMITMENT_CARD_DDL + INBOUND_OVERFLOW_DDL + INBOUND_PRESENTATION_DDL + PROJECT_ADVISORY_DDL + CONTACT_IDENTITY_CARD_DDL + DRAFT_CARD_DDL + STAGED_UPDATE_CARD_DDL + PERSONAL_REMINDER_DDL + DRAFT_ATTACHMENT_DDL

def apply_schema(conn: sqlite3.Connection) -> None:
    """Apply current DDL while deferring indexes incompatible with legacy tables."""
    from src.db.schema_version_guard import require_supported_schema_version

    require_supported_schema_version(conn)
    had_domain_tables = conn.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name != 'schema_version' LIMIT 1"
    ).fetchone() is not None
    _exec_schema_compatibly(conn)
    from src.db.memory_state_sequence_schema import ensure_memory_event_sequence
    ensure_memory_event_sequence(conn)
    from src.db.followup_schema import ensure_followup_action_columns
    ensure_followup_action_columns(conn)
    from src.db.project_advisory_schema import ensure_advisory_delivery_columns
    ensure_advisory_delivery_columns(conn)
    _ensure_processed_emails_mining_schema(conn)
    _ensure_llm_traces_schema(conn)
    _ensure_session_summaries_fts(conn)
    from src.learning.lifecycle_schema import ensure_lifecycle_schema
    ensure_lifecycle_schema(conn)
    from src.wiki.publication.schema import ensure_publication_schema
    ensure_publication_schema(conn)
    from src.fix_sessions.schema import ensure_fix_session_schema
    ensure_fix_session_schema(conn)
    from src.auth.schema import ensure_auth_schema
    ensure_auth_schema(conn)
    from src.email.workspace_schema import ensure_draft_workspace_schema
    ensure_draft_workspace_schema(conn)
    from src.assistant.schema import ensure_assistant_schema
    ensure_assistant_schema(conn)
    from config.defaults import CURRENT_SCHEMA_VERSION

    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    current = row[0] if row and row[0] is not None else 0
    if current < CURRENT_SCHEMA_VERSION and not had_domain_tables:
        conn.execute(
            "INSERT INTO schema_version (version) VALUES (?)",
            (CURRENT_SCHEMA_VERSION,),
        )
    conn.commit()


def _exec_schema_compatibly(conn: sqlite3.Connection) -> None:
    """Execute DDL, deferring objects incompatible with historical table shapes.

    ``CREATE TABLE IF NOT EXISTS`` deliberately preserves historical table shapes.
    A later ``CREATE INDEX`` in the same script can therefore reference a column
    that its owning migration has not added yet.  Startup runs ``apply_schema``
    before migrations, so defer those indexes until the migrated shape exists.
    """
    outbox_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(learning_outbox)")
    }
    legacy_learning_outbox = bool(outbox_columns) and "job_id" not in outbox_columns
    for statement in _schema_statements(SCHEMA_DDL):
        normalized = statement.lstrip().upper()
        if legacy_learning_outbox and normalized.startswith(
            "CREATE TABLE IF NOT EXISTS LEARNING_JOB_EFFECTS"
        ):
            logger.info("Deferring learning_job_effects until learning_outbox migration")
            continue
        try:
            conn.execute(statement)
        except sqlite3.OperationalError as exc:
            if _is_legacy_shape_index_error(statement, exc):
                logger.info("Deferring schema index until migration: %s", statement.split()[5])
                continue
            raise


def _schema_statements(script: str) -> list[str]:
    statements: list[str] = []
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            if statement:
                statements.append(statement)
            buffer = ""
    if buffer.strip():
        raise ValueError("SCHEMA_DDL contains an incomplete SQL statement")
    return statements


def _is_legacy_shape_index_error(statement: str, exc: sqlite3.OperationalError) -> bool:
    normalized = statement.lstrip().upper()
    return (
        normalized.startswith("CREATE INDEX IF NOT EXISTS ")
        or normalized.startswith("CREATE UNIQUE INDEX IF NOT EXISTS ")
    ) and "no such column:" in str(exc).lower()


def _ensure_processed_emails_mining_schema(conn: sqlite3.Connection) -> None:
    """Reconcile mining prerequisites before backfill on historical email tables."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(processed_emails)")}
    for name, typedef in [
        # Startup precedes v29, which originally introduced this flag.
        ("knowledge_mined", "INTEGER DEFAULT 0"),
        ("knowledge_mining_status", "TEXT NOT NULL DEFAULT 'pending'"),
        ("knowledge_mining_attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("knowledge_mining_error", "TEXT"),
        ("knowledge_mining_lease_owner", "TEXT"),
        ("knowledge_mining_lease_expires_at", "TEXT"),
        ("knowledge_mined_at", "TEXT"),
    ]:
        if name not in columns:
            conn.execute(f"ALTER TABLE processed_emails ADD COLUMN {name} {typedef}")
    conn.execute(
        """UPDATE processed_emails
           SET knowledge_mining_status = 'mined'
           WHERE knowledge_mined = 1 AND knowledge_mining_status != 'mined'"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_processed_emails_mining_due
           ON processed_emails(
               knowledge_mined, knowledge_mining_status,
               knowledge_mining_lease_expires_at, processed_at
           )"""
    )


def _ensure_llm_traces_schema(conn: sqlite3.Connection) -> None:
    """Upgrade llm_traces in-place when older CHECK constraints reject newer error classes."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'llm_traces'"
    ).fetchone()
    create_sql = (row[0] if row else "") or ""
    if "'guard'" in create_sql and "per_guard_results" in create_sql:
        return

    conn.execute("ALTER TABLE llm_traces RENAME TO llm_traces_old")
    conn.execute(
        """CREATE TABLE llm_traces (
            trace_id TEXT PRIMARY KEY,
            task TEXT NOT NULL,
            model TEXT NOT NULL,
            tokens_in INTEGER DEFAULT 0,
            tokens_out INTEGER DEFAULT 0,
            latency_ms INTEGER DEFAULT 0,
            output_valid INTEGER DEFAULT 1,
            error_class TEXT CHECK (error_class IN ('none','infra','schema','guard','timeout') OR error_class IS NULL),
            error_message TEXT,
            decision_outcome TEXT,
            prompt_hash TEXT,
            per_guard_results TEXT,
            trace_metadata TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    conn.execute(
        """INSERT INTO llm_traces
           (trace_id, task, model, tokens_in, tokens_out, latency_ms,
            output_valid, error_class, error_message, decision_outcome, created_at)
           SELECT trace_id, task, model, tokens_in, tokens_out, latency_ms,
                  output_valid, error_class, error_message, decision_outcome, created_at
           FROM llm_traces_old"""
    )
    conn.execute("DROP TABLE llm_traces_old")


def _ensure_session_summaries_fts(conn: sqlite3.Connection) -> None:
    """Create FTS5 virtual table + sync triggers for session_summaries (idempotent)."""
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS session_summaries_fts USING fts5(
            summary_text,
            projects_discussed,
            decisions_made,
            open_questions,
            content='session_summaries',
            content_rowid='rowid'
        )
    """)
    conn.executescript("""
        CREATE TRIGGER IF NOT EXISTS session_summaries_ai AFTER INSERT ON session_summaries BEGIN
            INSERT INTO session_summaries_fts(rowid, summary_text, projects_discussed, decisions_made, open_questions)
            VALUES (new.rowid, new.summary_text, new.projects_discussed, new.decisions_made, new.open_questions);
        END;

        CREATE TRIGGER IF NOT EXISTS session_summaries_ad AFTER DELETE ON session_summaries BEGIN
            INSERT INTO session_summaries_fts(session_summaries_fts, rowid, summary_text, projects_discussed, decisions_made, open_questions)
            VALUES ('delete', old.rowid, old.summary_text, old.projects_discussed, old.decisions_made, old.open_questions);
        END;

        CREATE TRIGGER IF NOT EXISTS session_summaries_au AFTER UPDATE ON session_summaries BEGIN
            INSERT INTO session_summaries_fts(session_summaries_fts, rowid, summary_text, projects_discussed, decisions_made, open_questions)
            VALUES ('delete', old.rowid, old.summary_text, old.projects_discussed, old.decisions_made, old.open_questions);
            INSERT INTO session_summaries_fts(rowid, summary_text, projects_discussed, decisions_made, open_questions)
            VALUES (new.rowid, new.summary_text, new.projects_discussed, new.decisions_made, new.open_questions);
        END;
    """)


def get_table_names(conn: sqlite3.Connection) -> list[str]:
    """Return all user table names in the database."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [row[0] for row in rows]
