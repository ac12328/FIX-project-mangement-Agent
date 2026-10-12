"""
Tool Registry — defines all tools available to the Lead Agent.
Each tool has a static contract (input/output schema, access control).
propose_truth_write is NEVER in the LLM tool list.

Design decision: Python dataclasses are used instead of JSON/YAML.
They are equally static and more Pythonic. Use to_dict() / to_json() for
inspection or serialisation. (Blueprint §13.1 intentional deviation.)
"""

from typing import Any

from src.tools.contracts import ToolContract


# Tool definitions

GET_PROJECT_TRUTH = ToolContract(
    name="get_project_truth",
    description="Get the current truth snapshot for a project",
    input_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
        },
        "required": ["project_id"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "project": {"type": "object"},
            "milestone_history": {"type": "array"},
        },
    },
    allowed_task_types=["*"],
    allowed_contexts=["*"],
    side_effect_type="read",
    timeout_seconds=10,
    retry_policy="once",
)

SEARCH_EMAILS = ToolContract(
    name="search_emails",
    description="Search processed emails for a project or contact",
    input_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "contact_email": {"type": "string"},
            "keyword": {"type": "string"},
            "limit": {"type": "integer", "default": 10},
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "emails": {"type": "array"},
            "total_count": {"type": "integer"},
        },
    },
    allowed_task_types=[
        "reply_to_client", "truth_write_reasoning", "answer_status",
        "general", "query", "email", "orchestrator", "chat",
        "research", "project_write", "email_to_truth", "draft",
    ],
    allowed_contexts=["*"],
    side_effect_type="read",
    timeout_seconds=15,
)

GET_WORKING_MEMORY = ToolContract(
    name="get_working_memory",
    description="Get working memory entries for a project",
    input_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
        },
        "required": ["project_id"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "memories": {"type": "array"},
        },
    },
    allowed_task_types=["*"],
    allowed_contexts=["*"],
    side_effect_type="read",
    timeout_seconds=10,
)

GET_RELATIONSHIP_PREFS = ToolContract(
    name="get_relationship_prefs",
    description="Get response pattern and relationship preferences for a contact+project pair",
    input_schema={
        "type": "object",
        "properties": {
            "contact_id": {"type": "string"},
            "project_id": {"type": "string"},
        },
        "required": ["contact_id"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "preferred_greeting": {"type": "string"},
            "tone": {"type": "string"},
            "avoid_phrases": {"type": "array"},
        },
    },
    allowed_task_types=[
        "reply_to_client", "draft_follow_up", "contact", "draft",
    ],
    side_effect_type="read",
    timeout_seconds=10,
)

DRAFT_REPLY = ToolContract(
    name="draft_reply",
    description=(
        "Generate a draft email. Use contact_id or to_email to specify the recipient. "
        "For inbound replies, recipient_mode defaults to the latest external thread sender. "
        "Pass exact approved wiki_paths to ground detailed client guidance. "
        "Pass doc_keys (from list_reference_docs) to attach standing files "
        "(e.g. conformance_test_plan). Conformance/guideline contexts auto-attach "
        "conformance_test_plan when doc_keys omitted. "
        "Never claim files are attached unless attachments are returned."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "Target project ID"},
            "contact_id": {
                "type": "string",
                "description": "Contact ID of the intended recipient. The draft To field will use this contact's email.",
            },
            "to_email": {
                "type": "string",
                "description": "Explicit recipient email address override. Takes priority over contact_id.",
            },
            "recipient_mode": {
                "type": "string",
                "enum": ["thread_sender", "project_primary", "explicit"],
                "description": (
                    "Recipient selection policy. Defaults to thread_sender for inbound replies. "
                    "Use explicit only with to_email or contact_id."
                ),
            },
            "thread_mode": {
                "type": "string",
                "enum": ["auto", "reply", "standalone"],
                "description": (
                    "Thread policy. auto creates a standalone message for an explicitly requested "
                    "internal handoff from an external client thread; otherwise it replies in-thread."
                ),
            },
            "cc": {
                "type": "string",
                "description": "Explicit CC recipients for standalone drafts. Thread replies inherit thread CC.",
            },
            "context": {
                "type": "string",
                "description": "What the email should communicate (key points, instructions to recipient).",
            },
            "signal_type": {
                "type": "string",
                "description": (
                    "Draft scenario: inbound_reply (default), b2b_launch_date_request "
                    "(ask client for go-live dates after production session online), "
                    "stale_external, overdue_followup, sla_deadline."
                ),
            },
            "tone": {"type": "string"},
            "doc_keys": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Reference doc keys to attach after draft creation "
                    "(e.g. ['conformance_test_plan']). Use list_reference_docs for keys."
                ),
            },
            "wiki_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Exact trusted Wiki page paths (without .md) whose complete body must ground "
                    "the draft. For PM UAT use "
                    "['playbook/conformance/public-demo-uat-guidelines']."
                ),
            },
        },
        "required": ["context"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "subject": {"type": "string"},
            "body": {"type": "string"},
            "confidence": {"type": "number"},
            "attachments": {"type": "array"},
            "attachments_failed": {"type": "array"},
            "attachment_outcome": {"type": "string", "enum": ["unknown"]},
        },
    },
    allowed_task_types=["reply_to_client", "draft_follow_up", "draft"],
    side_effect_type="write",
    timeout_seconds=30,
    retry_policy="none",
    fallback_behaviour="return_error",
    minimum_context_confidence=0.4,
)

PROPOSE_TRUTH_WRITE = ToolContract(
    name="propose_truth_write",
    description="Submit a ProposedUpdate to the Write Layer. NEVER called by LLM directly.",
    input_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "proposed_changes": {"type": "array"},
            "confidence": {"type": "number"},
            "risk_level": {"type": "string"},
        },
        "required": ["project_id", "proposed_changes"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "decision": {"type": "string"},
            "rejection_reasons": {"type": "array"},
        },
    },
    allowed_task_types=["truth_write_reasoning"],
    llm_visible=False,  # Critical: never exposed to LLM
    side_effect_type="write",
    requires_operator_approval=True,
    timeout_seconds=30,
    audit_required=True,
    minimum_context_confidence=0.4,
)

PUSH_TELEGRAM_REVIEW = ToolContract(
    name="push_telegram_review",
    description="Push a Review Card to operator Telegram when Write Layer decision is needs_review. Returns Telegram message ID for tracking operator response.",
    input_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "review_card": {
                "type": "object",
                "properties": {
                    "proposed_change_summary": {"type": "string"},
                    "reason": {"type": "string"},
                    "risk_level": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["proposed_change_summary", "risk_level"],
            },
        },
        "required": ["project_id", "review_card"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "telegram_message_id": {"type": "string"},
            "sent_at": {"type": "string"},
            "status": {"type": "string", "enum": ["sent", "failed"]},
        },
    },
    allowed_task_types=["truth_write_reasoning", "reply_to_client"],
    side_effect_type="external",
    timeout_seconds=15,
    retry_policy="none",
    audit_required=True,
    minimum_context_confidence=0.4,
)

LOG_EVIDENCE = ToolContract(
    name="log_evidence",
    description="Append a new evidence item to the Evidence Store for a project. Evidence Store is append-only and cannot be modified after write.",
    input_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "evidence_item": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["email", "commitment", "fact", "attachment", "manual_note"]},
                    "content_summary": {"type": "string"},
                    "source_ref": {"type": "string"},
                    "timestamp": {"type": "string"},
                },
                "required": ["type", "content_summary"],
            },
        },
        "required": ["project_id", "evidence_item"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "evidence_id": {"type": "string"},
            "appended_at": {"type": "string"},
        },
    },
    allowed_task_types=[
        "reply_to_client", "draft_follow_up", "truth_write_reasoning",
        "project_write", "email_to_truth",
    ],
    side_effect_type="write",
    timeout_seconds=15,
    audit_required=True,
)

THINK = ToolContract(
    name="think",
    description="Record your reasoning before taking action. Use this to plan your approach, note observations, or work through complex decisions. No side effects — the reasoning is preserved in conversation context for better follow-up.",
    input_schema={
        "type": "object",
        "properties": {
            "reasoning": {"type": "string", "description": "Your step-by-step reasoning or observations"},
        },
        "required": ["reasoning"],
    },
    output_schema={
        "type": "object",
        "properties": {"acknowledged": {"type": "string"}},
    },
    allowed_task_types=["*"],
    allowed_contexts=["*"],
    side_effect_type="write",
    timeout_seconds=1,
    query_safe=True,
)

FIND_INTERNAL_CONTACT = ToolContract(
    name="find_internal_contact",
    description="Find internal contacts by expertise area, ranked by responsiveness_score",
    input_schema={
        "type": "object",
        "properties": {
            "expertise_needed": {
                "type": "string",
                "description": "Expertise area needed (e.g. KYC review, network access)",
            },
            "limit": {"type": "integer", "default": 5},
        },
        "required": ["expertise_needed"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "contacts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "contact_id": {"type": "string"},
                        "display_name": {"type": "string"},
                        "department": {"type": "string"},
                        "expertise_tags": {"type": "array"},
                        "responsiveness_score": {"type": "number"},
                        "primary_email": {"type": "string"},
                    },
                },
            },
            "total_count": {"type": "integer"},
        },
    },
    allowed_task_types=["*"],
    allowed_contexts=["*"],
    side_effect_type="read",
    timeout_seconds=10,
)


GET_PROJECT_EMAILS = ToolContract(
    name="get_project_emails",
    description=(
        "Returns the email timeline for a project: inbound replies merged with processed email "
        "metadata, newest first. Processed rows include sender, CC, and thread fields. Exact "
        "[PROJECT_ID] subject tags are accepted for emails not yet linked by project_id."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "Project ID"},
            "limit": {"type": "integer", "description": "Max emails to return (default 10, max 20)"},
        },
        "required": ["project_id"],
    },
    output_schema={"type": "object", "properties": {"emails": {"type": "array"}}},
    allowed_task_types=["*"],
    allowed_contexts=["*"],
    side_effect_type="read",
    timeout_seconds=10,
)

GET_EMAIL_BODY = ToolContract(
    name="get_email_body",
    description=(
        "Returns email body plus sender, CC, and thread metadata. Use email_id from "
        "get_project_emails for processed emails or reply_id for inbound_replies. Project scope "
        "accepts either the linked project_id or an exact [PROJECT_ID] subject/body tag. "
        "Truncated at 4000 chars; use offset to read further."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "Project scope. Required when the agent has a project context."},
            "email_id": {"type": "string", "description": "email_id from get_project_emails (preferred)"},
            "reply_id": {"type": "string", "description": "reply_id from get_project_emails (legacy)"},
            "offset": {"type": "integer", "description": "Character offset for pagination (default 0)"},
        },
    },
    output_schema={"type": "object", "properties": {"body_text": {"type": "string"}}},
    allowed_task_types=["*"],
    allowed_contexts=["*"],
    side_effect_type="read",
    timeout_seconds=10,
)


# ─── Chat-only tools ────────────────────────────────────────
# These tools are primarily used by the Telegram chat agent.
# Some (query_projects, add_note, manage_followup) could be useful
# for the Lead Agent in the future — adjust allowed_contexts then.


# AI Workspace read tools. These are exposed through a narrow Workspace profile,
# not through generic SQL or unrestricted repository access.
ASSISTANT_PORTFOLIO_ATTENTION = ToolContract(
    name="portfolio_attention",
    description=(
        "Analyze which projects need attention using deterministic aging, deadline, blocked, "
        "unanswered-message, missing-next-action, and follow-up signals. Use this for portfolio "
        "priority, risk, focus, or 'this week' questions instead of query_projects."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            "include_on_track": {"type": "boolean", "default": False},
        },
    },
    output_schema={"type": "object", "properties": {"results": {"type": "array"}}},
    allowed_task_types=["*"],
    allowed_contexts=["web"],
    side_effect_type="read",
    timeout_seconds=15,
)

ASSISTANT_SEARCH_PROJECT_PARTIES = ToolContract(
    name="search_project_parties",
    description=(
        "Search the canonical project_parties to organisations graph by organisation/vendor name. "
        "Use this for questions asking which projects use a vendor, client, regulator, counterparty, "
        "or other organisation. Completed projects are included by default."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "party_name": {"type": "string"},
            "party_role": {
                "type": "string",
                "enum": ["client", "vendor", "regulator", "internal", "counterparty", "other"],
            },
            "include_terminal": {"type": "boolean", "default": True},
        },
        "required": ["party_name"],
    },
    output_schema={"type": "object", "properties": {"results": {"type": "array"}}},
    allowed_task_types=["*"],
    allowed_contexts=["web"],
    side_effect_type="read",
    timeout_seconds=10,
)

ASSISTANT_QUERY_PORTFOLIO = ToolContract(
    name="query_portfolio",
    description=(
        "List or filter projects with structured fields. Use status_transition for historical "
        "status changes; it reads accepted audit records and never infers transition time from "
        "projects.updated_at. Do not use this for attention/aging analysis or vendor relationships."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "search_text": {"type": "string"},
            "project_status": {"type": "string"},
            "current_milestone": {"type": "string"},
            "ball_with": {"type": "string"},
            "market": {"type": "string"},
            "client_name": {"type": "string"},
            "status_transition": {
                "type": "object",
                "description": "Accepted audit-backed project status transition over an inclusive date range.",
                "properties": {
                    "to_status": {
                        "type": "string",
                        "enum": ["Active", "Blocked", "Waiting", "Completed", "Archived", "Cancelled"],
                    },
                    "from": {"type": "string", "description": "Inclusive YYYY-MM-DD"},
                    "to": {"type": "string", "description": "Inclusive YYYY-MM-DD"},
                },
                "required": ["to_status", "from", "to"],
                "additionalProperties": False,
            },
            "include_terminal": {"type": "boolean", "default": False},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            "cursor": {"type": "string"},
        },
    },
    output_schema={"type": "object", "properties": {"results": {"type": "array"}}},
    allowed_task_types=["*"],
    allowed_contexts=["web"],
    side_effect_type="read",
    timeout_seconds=10,
)

ASSISTANT_GET_PROJECT_BRIEF = ToolContract(
    name="get_project_brief",
    description=(
        "Get a source-linked project brief containing authoritative current state, internal owner/ball, "
        "target date, next action, parties, milestone history, recent activity, and recent emails. "
        "Use this for status, recent changes, evidence conflicts, owner, and next-action questions."
    ),
    input_schema={
        "type": "object",
        "properties": {"project_id": {"type": "string"}},
        "required": ["project_id"],
    },
    output_schema={"type": "object", "properties": {"project": {"type": "object"}}},
    allowed_task_types=["*"],
    allowed_contexts=["web"],
    side_effect_type="read",
    timeout_seconds=15,
)

ASSISTANT_SEARCH_PROJECT_TIMELINE = ToolContract(
    name="search_project_timeline",
    description=(
        "Search a project chronology across processed emails, activity, milestone history, and "
        "evidence. Supports date, text, direction, thread, type filters, and cursor pagination."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "kinds": {
                "type": "array",
                "items": {"type": "string", "enum": ["email", "activity", "milestone", "evidence"]},
            },
            "from": {"type": "string", "description": "ISO date or timestamp, inclusive"},
            "to": {"type": "string", "description": "ISO date or timestamp, inclusive"},
            "query": {"type": "string"},
            "direction": {"type": "string", "enum": ["inbound", "outbound"]},
            "thread_id": {"type": "string"},
            "cursor": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        "required": ["project_id"],
    },
    output_schema={"type": "object", "properties": {"results": {"type": "array"}}},
    allowed_task_types=["*"],
    allowed_contexts=["web"],
    side_effect_type="read",
    timeout_seconds=20,
)

ASSISTANT_LIST_PROJECT_WORK = ToolContract(
    name="list_project_work",
    description=(
        "List normalized project follow-ups, internal actions, personal work, and commitment "
        "evidence with due dates, status, overdue state, and provenance quality."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "kinds": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": ["followup", "internal_action", "personal_work", "commitment"],
                },
            },
            "status": {"type": "string", "enum": ["all", "open", "completed", "cancelled"]},
            "cursor": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        "required": ["project_id"],
    },
    output_schema={"type": "object", "properties": {"results": {"type": "array"}}},
    allowed_task_types=["*"],
    allowed_contexts=["web"],
    side_effect_type="read",
    timeout_seconds=20,
)

ASSISTANT_LIST_UNANSWERED_THREADS = ToolContract(
    name="list_unanswered_threads",
    description=(
        "List conservative unanswered candidates where the latest message in a resolved email "
        "thread is inbound and no newer outbound message exists."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "min_age_hours": {"type": "number", "minimum": 0, "default": 24},
            "include_terminal": {"type": "boolean", "default": False},
            "cursor": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
    },
    output_schema={"type": "object", "properties": {"results": {"type": "array"}}},
    allowed_task_types=["*"],
    allowed_contexts=["web"],
    side_effect_type="read",
    timeout_seconds=30,
)

ASSISTANT_GET_PROJECT_EVIDENCE_AUDIT = ToolContract(
    name="get_project_evidence_audit",
    description=(
        "Audit one project's authoritative current truth against persisted evidence, milestone "
        "history, and accepted, rejected, deferred, or superseded proposed updates."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        "required": ["project_id"],
    },
    output_schema={"type": "object", "properties": {"current_truth": {"type": "object"}}},
    allowed_task_types=["*"],
    allowed_contexts=["web"],
    side_effect_type="read",
    timeout_seconds=20,
)

ASSISTANT_ASSESS_MILESTONE_READINESS = ToolContract(
    name="assess_milestone_readiness",
    description=(
        "Assess milestone readiness from the active milestone contract and the latest valid, "
        "persisted, source-bound structured assessment. Never infers readiness from arbitrary email text."
    ),
    input_schema={
        "type": "object",
        "properties": {"project_id": {"type": "string"}},
        "required": ["project_id"],
    },
    output_schema={"type": "object", "properties": {"assessment_status": {"type": "string"}}},
    allowed_task_types=["*"],
    allowed_contexts=["web"],
    side_effect_type="read",
    timeout_seconds=20,
)

ASSISTANT_WEB_SEARCH = ToolContract(
    name="web_search",
    description=(
        "Search external web sources. This tool is available only when the trusted Workspace thread "
        "snapshot has Web Search enabled and a provider is configured."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 8},
        },
        "required": ["query"],
    },
    output_schema={"type": "object", "properties": {"sources": {"type": "array"}}},
    allowed_task_types=["*"],
    allowed_contexts=["web"],
    side_effect_type="read",
    timeout_seconds=45,
)

CHAT_QUERY_PROJECTS = ToolContract(
    name="query_projects",
    description=(
        "Search and filter projects. By default returns only active/ongoing projects (Active, Blocked, Waiting). "
        "Set include_all=true only when the user explicitly asks to see all projects including closed, archived, or cancelled ones."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "search_text": {"type": "string", "description": "Free text search across project ID and client name"},
            "project_status": {"type": "string", "description": "Filter: Active, Blocked, Waiting, Completed, Archived, Cancelled"},
            "ball_with": {"type": "string", "description": "Filter: Internal, External, Shared"},
            "current_milestone": {"type": "string", "description": "Filter: M1_NetworkSetup through M8_GoLive"},
            "include_all": {"type": "boolean", "description": "Set true only when user explicitly wants all projects including completed/archived/cancelled. Default false."},
            "min_days_stale": {
                "type": "integer",
                "description": "Only return projects whose last update was at least this many days ago. E.g. 3 returns projects not updated in the last 3 days."
            },
            "order_by_staleness": {
                "type": "boolean",
                "description": "If true, sort results by updated_at ascending (most stale first). Default false."
            },
            "milestone_gte": {
                "type": "string",
                "description": "Only return projects at or beyond this milestone. E.g. 'M5_ConformanceTestCases' returns M5, M6, M7, M8."
            },
            "has_overdue_followups": {
                "type": "boolean",
                "description": "If true, only return projects that have at least one overdue internal followup."
            },
        },
    },
    output_schema={"type": "object", "properties": {"results": {"type": "array"}, "total": {"type": "integer"}}},
    allowed_task_types=["*"],
    allowed_contexts=["chat"],
    side_effect_type="read",
    timeout_seconds=10,
)

CHAT_GET_PROJECT_STATUS = ToolContract(
    name="get_project_status",
    description="Get detailed status for a single project by its project ID.",
    input_schema={
        "type": "object",
        "properties": {"project_id": {"type": "string", "description": "The project ID to look up"}},
        "required": ["project_id"],
    },
    output_schema={"type": "object", "properties": {"project": {"type": "object"}}},
    allowed_task_types=["*"],
    allowed_contexts=["chat"],
    side_effect_type="read",
    timeout_seconds=10,
)

CHAT_CREATE_PROJECT = ToolContract(
    name="create_project",
    description=(
        "Stage a NEW project intake for operator confirmation. "
        "Use when the operator wants to set up / onboard a new FIX client project. "
        "Does NOT create the project until confirm_create_project is called after explicit operator OK. "
        "Required: project_id, client_name. Optional: client_email, market, connection_type, "
        "broker_code, target_go_live, vendor_email, notes. "
        "Alternative slash command: /newproject <ID> <ClientName> [email]."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "project_id": {
                "type": "string",
                "description": "New project ID (alphanumeric, 2–32 chars)",
            },
            "client_name": {
                "type": "string",
                "description": "Client company name, e.g. Demo Client",
            },
            "client_email": {
                "type": "string",
                "description": "Primary client contact email",
            },
            "market": {
                "type": "string",
                "description": "Market(s), e.g. HK, PM, US, or 'PM, US'",
            },
            "connection_type": {
                "type": "string",
                "description": "FIX connection type, default FIX 4.2",
            },
            "broker_code": {
                "type": "string",
                "description": "Broker / counterparty code if known",
            },
            "target_go_live": {
                "type": "string",
                "description": "Target go-live date (ISO preferred, e.g. 2026-08-31)",
            },
            "vendor_email": {
                "type": "string",
                "description": "Vendor/tech contact email (stored in notes)",
            },
            "notes": {
                "type": "string",
                "description": "Optional free-text notes for the project",
            },
        },
        "required": ["project_id", "client_name"],
    },
    output_schema={"type": "object", "properties": {"text": {"type": "string"}}},
    allowed_task_types=["*"],
    allowed_contexts=["chat"],
    side_effect_type="write",
    requires_operator_approval=True,
    timeout_seconds=15,
)

CHAT_CONFIRM_CREATE_PROJECT = ToolContract(
    name="confirm_create_project",
    description=(
        "Create a project that was staged via create_project. "
        "ONLY call after the operator explicitly confirms the proposed intake summary."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "project_id": {
                "type": "string",
                "description": "Project ID of the staged intake to commit",
            },
        },
        "required": ["project_id"],
    },
    output_schema={"type": "object", "properties": {"text": {"type": "string"}}},
    allowed_task_types=["*"],
    allowed_contexts=["chat"],
    side_effect_type="write",
    timeout_seconds=30,
)

CHAT_CANCEL_CREATE_PROJECT = ToolContract(
    name="cancel_create_project",
    description=(
        "Discard a staged new-project intake without creating the project."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "project_id": {
                "type": "string",
                "description": "Project ID of the staged intake to discard",
            },
        },
        "required": ["project_id"],
    },
    output_schema={"type": "object", "properties": {"text": {"type": "string"}}},
    allowed_task_types=["*"],
    allowed_contexts=["chat"],
    side_effect_type="write",
    timeout_seconds=10,
)

CHAT_UPDATE_PROJECT = ToolContract(
    name="update_project",
    description=(
        "Update ONE field of a project through the Write Layer. "
        "Use for single-field changes. For multiple fields, use update_project_batch instead."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "The project to update"},
            "field": {"type": "string", "description": "Exact field name. Valid: project_status, ball_with, current_milestone, next_action, notes, target_go_live, client_name, client_email, broker_code, connection_type, sla_limit, market, project_tier"},
            "new_value": {"type": "string", "description": "New value. Enums — project_status: Active/Blocked/Waiting/Completed/Archived/Cancelled; ball_with: Internal/External/Shared/Unknown; current_milestone: M1_NetworkSetup through M8_GoLive; project_tier: high/medium/low; others: any string"},
            "reason": {"type": "string", "description": "WHY this change is proposed"},
            "evidence_ref": {"type": "string", "description": "REQUIRED when current_milestone jumps more than 1 step (e.g. M5→M7). Use the evidence_id returned by add_note, or the email_id of the source email. Without this, milestone jumps >1 step will be REJECTED."},
        },
        "required": ["project_id", "field", "new_value"],
    },
    output_schema={"type": "object", "properties": {"status": {"type": "string"}}},
    allowed_task_types=["*"],
    allowed_contexts=["chat"],
    side_effect_type="write",
    timeout_seconds=30,
    minimum_context_confidence=0.4,
)

CHAT_UPDATE_PROJECT_BATCH = ToolContract(
    name="update_project_batch",
    description=(
        "Update MULTIPLE fields of a project in one call. "
        "Each change is validated by guards independently. "
        "See update_project for valid field names and enum values."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "The project to update"},
            "changes": {
                "type": "array",
                "items": {"type": "object", "properties": {"field": {"type": "string"}, "new_value": {"type": "string"}}, "required": ["field", "new_value"]},
                "description": "List of {field, new_value} pairs to apply",
            },
            "reason": {"type": "string", "description": "WHY these changes are proposed"},
            "evidence_ref": {"type": "string", "description": "REQUIRED when current_milestone jumps more than 1 step. Use the evidence_id from add_note or the email_id. Without this, milestone jumps >1 will be REJECTED."},
        },
        "required": ["project_id", "changes", "reason"],
    },
    output_schema={"type": "object", "properties": {"status": {"type": "string"}}},
    allowed_task_types=["*"],
    allowed_contexts=["chat"],
    side_effect_type="write",
    timeout_seconds=30,
    minimum_context_confidence=0.4,
)

CHAT_ADD_CONTACT = ToolContract(
    name="add_contact",
    description="Register or update a client contact in the Contact Registry and link to a project.",
    input_schema={
        "type": "object",
        "properties": {
            "email": {"type": "string", "description": "Contact email address"},
            "name": {"type": "string", "description": "Contact display name"},
            "project_id": {"type": "string", "description": "The project this contact belongs to"},
            "role": {"type": "string", "description": "Optional role (IT, Trader, Manager)"},
        },
        "required": ["email", "project_id"],
    },
    output_schema={"type": "object", "properties": {"result": {"type": "string"}}},
    allowed_task_types=["*"],
    allowed_contexts=["chat"],
    side_effect_type="write",
    timeout_seconds=15,
)

CHAT_LIST_CONTACTS = ToolContract(
    name="list_contacts",
    description="List all contacts registered for a project.",
    input_schema={
        "type": "object",
        "properties": {"project_id": {"type": "string", "description": "The project to list contacts for"}},
        "required": ["project_id"],
    },
    output_schema={"type": "object", "properties": {"contacts": {"type": "array"}}},
    allowed_task_types=["*"],
    allowed_contexts=["chat"],
    side_effect_type="read",
    timeout_seconds=10,
)

CHAT_ADD_NOTE = ToolContract(
    name="add_note",
    description="Record a project note (meeting, call summary, observation). Stored permanently as evidence.",
    input_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "The project this note relates to"},
            "note": {"type": "string", "description": "The note text"},
        },
        "required": ["project_id", "note"],
    },
    output_schema={"type": "object", "properties": {"result": {"type": "string"}}},
    allowed_task_types=["*"],
    allowed_contexts=["chat"],
    side_effect_type="write",
    timeout_seconds=15,
)

CHAT_MANAGE_FOLLOWUP = ToolContract(
    name="manage_followup",
    description="Manage internal follow-up tasks. action=create (needs description and optional due_date YYYY-MM-DD); action=list returns the current revision; action=complete/cancel needs followup_id plus that expected_revision.",
    input_schema={
        "type": "object",
        "properties": {
            "action": {"type": "string", "description": "create | complete | cancel | list"},
            "project_id": {"type": "string", "description": "The project"},
            "description": {"type": "string", "description": "Task description (create)"},
            "due_date": {"type": "string", "description": "YYYY-MM-DD (create)"},
            "followup_id": {"type": "string", "description": "Follow-up ID (complete/cancel)"},
            "resolution_note": {"type": "string", "description": "Optional note (complete)"},
            "expected_revision": {"type": "integer", "minimum": 0, "description": "Current action revision from list; required for complete/cancel"},
        },
        "required": ["action", "project_id"],
    },
    output_schema={"type": "object", "properties": {"result": {"type": "string"}}},
    allowed_task_types=["*"],
    allowed_contexts=["chat"],
    side_effect_type="write",
    timeout_seconds=15,
)


# ─── On-demand context tools (P05) ──────────────────────────
# These let the LLM pull context layers selectively instead of
# front-loading everything into the prompt.

LOAD_PROJECT_CONTEXT = ToolContract(
    name="load_project_context",
    description=(
        "Load specific context layers for a project on demand. "
        "Layers: truth (project record + milestone history), memory (working memory), "
        "relationship (contact preferences + tone), evidence (recent evidence items), "
        "knowledge (market-specific rules). Load only the layers you need."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "The project ID"},
            "layers": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": ["truth", "memory", "relationship", "evidence", "knowledge"],
                },
                "description": "Which layers to load. Omit to load all.",
            },
            "contact_id": {
                "type": "string",
                "description": "Contact ID (needed for relationship layer)",
            },
        },
        "required": ["project_id"],
    },
    output_schema={"type": "object"},
    allowed_task_types=["*"],
    allowed_contexts=["*"],
    side_effect_type="read",
    timeout_seconds=15,
)

WIKI_SEARCH = ToolContract(
    name="wiki_search",
    description=(
        "Search the curated wiki knowledge base. Returns structured hits "
        "(path, title, summary, confidence, status, snippet) plus a formatted results string. "
        "Aliases are expanded (P-mart, FINI, DTC, etc.). Prefer citing hit paths. "
        "Use for domain knowledge queries "
        "(e.g. 'Demo Gateway A setup quirks', 'HK market routing', 'HKIDR requirements')."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Topic to search in the wiki",
            },
            "top_k": {
                "type": "integer",
                "description": "Number of results to return (default 5)",
                "default": 5,
            },
            "domain": {
                "type": "string",
                "enum": ["b2b", "fix-connectivity", "conformance", "settlement", "internal-ops", "general"],
                "description": "Optional domain ranking bias.",
            },
            "project_id": {
                "type": "string",
                "description": "Optional project scope. Project-specific pages for other projects are excluded.",
            },
            "market": {
                "type": "string",
                "description": "Optional market scope such as PM, HK, US, JP, SG, or CN.",
            },
            "mode": {
                "type": "string",
                "enum": ["internal", "project_context", "external_draft", "curator"],
                "default": "internal",
                "description": "Retrieval policy mode. Use project_context with project_id for project work.",
            },
        },
        "required": ["query"],
    },
    output_schema={"type": "object", "properties": {"results": {"type": "string"}}},
    allowed_task_types=["*"],
    allowed_contexts=["*"],
    side_effect_type="read",
    timeout_seconds=10,
)

WIKI_CREATE = ToolContract(
    name="wiki_create",
    description=(
        "Create a new durable wiki page (rare). Prefer wiki_update to enrich existing pages. "
        "Paths use singular dirs: entity/, concept/, pattern/, playbook/, markets/, insight/, sources/. "
        "Body must start with a **bold one-sentence summary**. "
        "If similar pages exist, returns action=enrich_existing unless force_create=true. "
        "See wiki/WIKI_GUIDE.md."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Relative path without extension, e.g. 'concept/fix_session' or 'playbook/settlement/ssi'",
            },
            "title": {"type": "string"},
            "page_type": {
                "type": "string",
                "enum": ["entity", "market", "concept", "pattern", "playbook", "insight", "source"],
            },
            "body": {
                "type": "string",
                "description": "Markdown body; must start with **one-sentence summary.**",
            },
            "tags": {"type": "array", "items": {"type": "string"}},
            "sources": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Locators e.g. email:id, doc:path, operator:name@date",
            },
            "force_create": {
                "type": "boolean",
                "description": "Skip enrich-existing gate when true (new durable entity only)",
                "default": False,
            },
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "links": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "target": {"type": "string"},
                        "rel": {"type": "string"},
                        "note": {"type": "string"},
                    },
                    "required": ["target", "rel"],
                },
            },
        },
        "required": ["path", "title", "page_type", "body"],
    },
    output_schema={"type": "object"},
    allowed_task_types=["*"],
    allowed_contexts=["*"],
    side_effect_type="write",
    timeout_seconds=10,
    audit_required=True,
)

WIKI_UPDATE = ToolContract(
    name="wiki_update",
    description=(
        "Enrich an existing wiki page (preferred over create). Append content, replace body, "
        "add sources/links, or change confidence. Always wiki_read first. "
        "replace_body must keep a **bold summary** first line."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Relative path without extension"},
            "replace_body": {
                "type": "string",
                "description": (
                    "Replace the ENTIRE body of the page with this text. "
                    "Takes priority over append_body if both are provided. "
                    "Must start with **summary.** Use wiki_read first."
                ),
            },
            "append_body": {"type": "string", "description": "Text to append to the page body"},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "add_sources": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Source locators to append",
            },
            "add_links": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "target": {"type": "string"},
                        "rel": {"type": "string"},
                        "note": {"type": "string"},
                    },
                    "required": ["target", "rel"],
                },
            },
        },
        "required": ["path"],
    },
    output_schema={"type": "object"},
    allowed_task_types=["*"],
    allowed_contexts=["*"],
    side_effect_type="write",
    timeout_seconds=10,
    audit_required=True,
)

WIKI_READ = ToolContract(
    name="wiki_read",
    description=(
        "Read a wiki page by path. Returns summary, body, sources, typed links, confidence, status. "
        "Always use this before wiki_update. "
        "Path: 'category/stem' e.g. 'markets/jp_market', 'entity/demo_organization', 'playbook/settlement/ssi'."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Relative path without extension, e.g. 'markets/jp_market'",
            },
        },
        "required": ["path"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "title": {"type": "string"},
            "body": {"type": "string"},
            "summary": {"type": "string"},
            "page_type": {"type": "string"},
            "sources": {"type": "array", "items": {"type": "string"}},
            "links": {"type": "array"},
            "confidence": {"type": "string"},
            "status": {"type": "string"},
            "updated": {"type": "string"},
        },
    },
    allowed_task_types=["*"],
    allowed_contexts=["*"],
    side_effect_type="read",
    timeout_seconds=5,
)

WIKI_LINK = ToolContract(
    name="wiki_link",
    description=(
        "Add a typed relationship from one wiki page to another. "
        "Relationship types: depends_on, relates_to, operates_in, uses, requires, "
        "supersedes, contradicts (triggers operator notification), derived_from."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "from_path": {"type": "string"},
            "to_path": {"type": "string"},
            "rel": {
                "type": "string",
                "enum": [
                    "depends_on", "depended_by", "relates_to", "operates_in",
                    "uses", "used_by", "requires", "supersedes",
                    "contradicts", "derived_from",
                ],
            },
            "note": {"type": "string"},
        },
        "required": ["from_path", "to_path", "rel"],
    },
    output_schema={"type": "object"},
    allowed_task_types=["*"],
    allowed_contexts=["*"],
    side_effect_type="write",
    timeout_seconds=10,
    audit_required=True,
)

# ─── Reference document tools ───────────────────────────────

LIST_REFERENCE_DOCS = ToolContract(
    name="list_reference_docs",
    description=(
        "List all standing reference documents available to attach to emails. "
        "Returns doc_key, filename, and description for each document."
    ),
    input_schema={"type": "object", "properties": {}},
    output_schema={
        "type": "object",
        "properties": {
            "docs": {"type": "array"},
        },
    },
    allowed_task_types=["*"],
    allowed_contexts=["*"],
    side_effect_type="read",
    timeout_seconds=5,
)

ATTACH_REFERENCE_DOC = ToolContract(
    name="attach_reference_doc",
    description=(
        "Attach a standing reference document to a draft email. "
        "Call this AFTER draft_reply returns a draft_id. "
        "Use list_reference_docs to see available doc_keys."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "draft_id": {"type": "string", "description": "The draft_id returned by draft_reply"},
            "doc_key": {"type": "string", "description": "Document key from list_reference_docs"},
        },
        "required": ["draft_id", "doc_key"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "draft_id": {"type": "string"},
            "attachment_outcome": {"type": "string", "enum": ["unknown"]},
            "side_effect_status": {"type": "string"},
            "filename": {"type": "string"},
            "size": {"type": "integer"},
            "error": {"type": "string"},
        },
    },
    allowed_task_types=["*"],
    allowed_contexts=["*"],
    side_effect_type="write",
    timeout_seconds=15,
    retry_policy="once",
    idempotency_mode="trusted_key",
    fallback_behaviour="return_error",
)

READ_ARTIFACT = ToolContract(
    name="read_artifact",
    description=(
        "Load full content for a thread artifact pointer returned in a ToolDigest. "
        "Use only when the digest summary is insufficient."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "artifact_id": {"type": "string", "description": "UUID from ToolDigest.artifact_id"},
        },
        "required": ["artifact_id"],
    },
    output_schema={"type": "object"},
    allowed_task_types=["*"],
    allowed_contexts=["*"],
    side_effect_type="read",
    timeout_seconds=5,
    query_safe=True,
)

# ─── Subagent tool (P11) ────────────────────────────────────

DELEGATE = ToolContract(
    name="delegate",
    description=(
        "Spawn a specialist skill with isolated context and a restricted tool set. "
        "ALL write operations must go through delegate — host cannot call write tools directly. "
        "Prefer skill_id from the skill registry. "
        "Skills: new_project, email_to_truth, project_write, followup, contact, draft, "
        "support, research, status, portfolio, email, memory, personal_work. "
        "Prefer ONE skill per operator intent (e.g. email_to_truth, not research+write). "
        "If a second stage needs prior tool evidence, pass resume_from=<session_id> from "
        "the previous delegate result (same skill always; cross-skill allowlist: "
        "research→draft|project_write|email_to_truth, email_to_truth→draft, status→draft|project_write). "
        "Host policy: max 2 fresh delegates/turn; max 1 fresh write skill; after research/status, "
        "follow-on draft/write requires resume_from. "
        "Examples: skill_id=email_to_truth to update from Demo Provisioning email; "
        "skill_id=new_project for intake; skill_id=draft with resume_from after research."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "Clear instruction for the specialist (include all operator details)",
            },
            "project_id": {
                "type": "string",
                "description": "Optional project ID to focus the specialist on",
            },
            "skill_id": {
                "type": "string",
                "enum": [
                    "new_project",
                    "email_to_truth",
                    "project_write",
                    "followup",
                    "contact",
                    "draft",
                    "support",
                    "research",
                    "status",
                    "portfolio",
                    "email",
                    "memory",
                    "personal_work",
                ],
                "description": (
                    "Registered skill to invoke (preferred). "
                    "Maps to specialist type + tool allowlist + skill prompt."
                ),
            },
            "resume_from": {
                "type": "string",
                "description": (
                    "session_id from a prior delegate result. Continues that specialist "
                    "transcript so tool evidence is not lost. Omit for a fresh spawn."
                ),
            },
            "specialist_type": {
                "type": "string",
                "enum": ["status", "research", "write", "draft", "portfolio", "support", "email"],
                "description": (
                    "Legacy specialist selector if skill_id omitted. "
                    "Defaults to research. Prefer skill_id."
                ),
                "default": "research",
            },
        },
        "required": ["task"],
    },
    output_schema={"type": "object"},
    allowed_task_types=["*"],
    allowed_contexts=["*"],
    side_effect_type="read",  # orchestration only; writes happen inside the skill
    timeout_seconds=60,
)


# ── Learning Loop Tools ──────────────────────────────────────────
AGENT_MEMORY = ToolContract(
    name="agent_memory",
    description=(
        "Personal memory system for the agent. "
        "Stores notes that persist across sessions. "
        "ONLY for operator preferences and corrections — never project-specific events. "
        "Space is limited (15000 chars) — replace outdated notes to make room. "
        "Use note_type to categorize: preference, pattern, correction, knowledge."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "add", "replace", "remove"],
                "description": "Action to perform on agent notes.",
            },
            "key": {
                "type": "string",
                "description": "Note key (required for add/replace/remove).",
            },
            "value": {
                "type": "string",
                "description": "Note value (required for add/replace).",
            },
            "note_type": {
                "type": "string",
                "enum": ["preference", "pattern", "correction", "knowledge"],
                "description": "Category for the note (used for add/replace/list filter). Default: knowledge.",
            },
        },
        "required": ["action"],
    },
    output_schema={"type": "object", "properties": {"text": {"type": "string"}}},
    allowed_task_types=["*"],
    allowed_contexts=["*"],
    side_effect_type="write",
    timeout_seconds=10,
)

SESSION_SEARCH = ToolContract(
    name="session_search",
    description=(
        "Search past conversation sessions by keyword. "
        "Returns matching session summaries with snippets, projects discussed, and decisions made. "
        "Use when the operator references a past conversation or you need to recall how something was handled."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Free-text search query (e.g. 'conformance test failure', 'VPN setup').",
            },
        },
        "required": ["query"],
    },
    output_schema={"type": "object", "properties": {"text": {"type": "string"}}},
    allowed_task_types=["*"],
    allowed_contexts=["*"],
    side_effect_type="read",
    timeout_seconds=10,
)

# ─── Confirmation & Undo Tools (WP-S1.1, WP-S1.2) ──────────

CHAT_CONFIRM_UPDATE = ToolContract(
    name="confirm_update",
    description=(
        "Apply a previously staged project update. "
        "Only call this AFTER the operator explicitly agrees to the proposed changes."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "The project with a pending staged update"},
        },
        "required": ["project_id"],
    },
    output_schema={"type": "object", "properties": {"text": {"type": "string"}}},
    allowed_task_types=["*"],
    allowed_contexts=["chat"],
    side_effect_type="write",
    timeout_seconds=30,
    minimum_context_confidence=0.4,
)

CHAT_CANCEL_UPDATE = ToolContract(
    name="cancel_update",
    description=(
        "Discard a previously staged project update without applying it."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "The project with a pending staged update"},
        },
        "required": ["project_id"],
    },
    output_schema={"type": "object", "properties": {"text": {"type": "string"}}},
    allowed_task_types=["*"],
    allowed_contexts=["chat"],
    side_effect_type="write",
    timeout_seconds=10,
)

CHAT_UNDO_UPDATE = ToolContract(
    name="undo_update",
    description=(
        "Undo the most recent accepted project update by reverting fields to their previous values. "
        "Only call when the operator explicitly requests an undo."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "The project to undo the last update for"},
        },
        "required": ["project_id"],
    },
    output_schema={"type": "object", "properties": {"text": {"type": "string"}}},
    allowed_task_types=["*"],
    allowed_contexts=["chat"],
    side_effect_type="write",
    timeout_seconds=30,
    minimum_context_confidence=0.4,
)

# ─── Activity & Draft Tools (WP-S2.3, WP-S2.4) ─────────────

CHAT_GET_ACTIVITY_LOG = ToolContract(
    name="get_activity_log",
    description=(
        "Get the recent activity log for a project. "
        "Shows what happened: updates, draft approvals, follow-ups, etc."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "The project to get activity for"},
            "limit": {"type": "integer", "description": "Max entries to return (default 20, max 50)"},
            "action_type": {"type": "string", "description": "Optional filter by action type"},
        },
        "required": ["project_id"],
    },
    output_schema={"type": "object", "properties": {"text": {"type": "string"}}},
    allowed_task_types=["*"],
    allowed_contexts=["chat"],
    side_effect_type="read",
    timeout_seconds=10,
)

CHAT_LIST_DRAFTS = ToolContract(
    name="list_drafts",
    description=(
        "List email drafts pending operator review, optionally filtered by project and status. "
        "Use to answer 'what drafts are waiting for my review?'"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "Optional project filter"},
            "status": {"type": "string", "description": "Optional status filter (e.g. 'pending_review', 'approved', 'sent')"},
            "limit": {"type": "integer", "description": "Max entries (default 10, max 20)"},
        },
        "required": [],
    },
    output_schema={"type": "object", "properties": {"text": {"type": "string"}}},
    allowed_task_types=["*"],
    allowed_contexts=["chat"],
    side_effect_type="read",
    timeout_seconds=10,
)

CHAT_GET_DRAFT = ToolContract(
    name="get_draft",
    description="Get a single draft including its full body text.",
    input_schema={
        "type": "object",
        "properties": {
            "draft_id": {"type": "string", "description": "The draft ID to retrieve"},
        },
        "required": ["draft_id"],
    },
    output_schema={"type": "object", "properties": {"text": {"type": "string"}}},
    allowed_task_types=["*"],
    allowed_contexts=["chat"],
    side_effect_type="read",
    timeout_seconds=10,
)

CHAT_EDIT_DRAFT = ToolContract(
    name="edit_draft",
    description=(
        "Edit an existing draft's body, subject, recipient (to_email), or CC. "
        "Use when operator asks to change/update a pending draft instead of creating a new one."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "draft_id": {"type": "string", "description": "The draft ID to edit"},
            "new_body": {"type": "string", "description": "New email body text"},
            "new_subject": {"type": "string", "description": "New subject line"},
            "to_email": {"type": "string", "description": "New recipient email address"},
            "cc": {"type": "string", "description": "New CC recipients (comma-separated)"},
        },
        "required": ["draft_id"],
    },
    output_schema={"type": "object", "properties": {"text": {"type": "string"}}},
    allowed_task_types=["*"],
    allowed_contexts=["chat"],
    side_effect_type="write",
    timeout_seconds=10,
)

CHAT_DISCARD_DRAFT = ToolContract(
    name="discard_draft",
    description=(
        "Discard a pending draft. Use when operator wants to remove a draft "
        "or when replacing it with a completely new one."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "draft_id": {"type": "string", "description": "The draft ID to discard"},
        },
        "required": ["draft_id"],
    },
    output_schema={"type": "object", "properties": {"text": {"type": "string"}}},
    allowed_task_types=["*"],
    allowed_contexts=["chat"],
    side_effect_type="write",
    timeout_seconds=10,
)

_EMAIL_ASSESSMENT_CITATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "source_type": {"type": "string", "enum": ["email", "attachment"]},
        "source_ref": {"type": "string", "minLength": 1},
        "quote": {
            "type": "string",
            "minLength": 1,
            "description": "Exact verbatim quote from the bounded email body or attachment text",
        },
        "actor": {"type": ["string", "null"]},
        "event_at": {"type": ["string", "null"], "format": "date-time"},
        "evidence_level": {
            "type": "string",
            "enum": [
                "L1_operator_confirmed",
                "L2_authoritative_system",
                "L3_named_authority_written",
                "L4_trusted_party_written",
                "L5_unverified_or_indirect",
            ],
        },
        "polarity": {"type": "string", "enum": ["positive", "negative"]},
        "modality": {
            "type": "string",
            "enum": [
                "confirmed",
                "in_progress",
                "planned",
                "conditional",
                "quoted_history",
                "unknown",
            ],
        },
    },
    "required": [
        "source_type",
        "source_ref",
        "quote",
        "evidence_level",
        "polarity",
        "modality",
    ],
}

_EMAIL_ASSESSMENT_CONFIDENCE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "extraction": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "project_identity": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "source_reliability": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "temporal_certainty": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "criteria_coverage": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "contradiction_score": {"type": "number", "minimum": 0.0, "maximum": 1.0},
    },
    "required": [
        "extraction",
        "project_identity",
        "source_reliability",
        "temporal_certainty",
        "criteria_coverage",
        "contradiction_score",
    ],
}


REPORT_EMAIL_ANALYSIS = ToolContract(
    name="report_email_analysis",
    description=(
        "Report your structured analysis of an inbound email. "
        "Call this once after analyzing the email. If it returns a retryable "
        "tool_arguments_schema_invalid error, correct only the listed schema paths and "
        "retry exactly once. Write summary and suggested_action "
        "in the operator's preferred response language (en / zh-TW / zh-CN per "
        "OPERATOR LANGUAGE POLICY), plus confidence score and any proposed project field changes."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "Project ID this email relates to"},
            "analysis_ref": {
                "type": "string",
                "description": "Required correlation token copied from the inbound analysis input (normally the email ID)",
            },
            "summary": {
                "type": "string",
                "description": (
                    "Brief summary of email content in the operator's preferred "
                    "response language (en / zh-TW / zh-CN)"
                ),
            },
            "suggested_action": {
                "type": "string",
                "description": (
                    "Recommended next action in the operator's preferred "
                    "response language (en / zh-TW / zh-CN)"
                ),
            },
            "confidence": {"type": "number", "description": "Confidence 0.0-1.0 in the analysis"},
            "proposed_fields": {
                "type": "object",
                "description": "Project fields to update: current_milestone, ball_with, next_action, project_status, target_go_live, sla_limit, project_tier",
            },
            "commitment_detected": {"type": "boolean", "description": "Whether a client commitment with due date was found"},
            "email_intent": {
                "type": "string",
                "enum": [
                    "client_internal_delegation",
                    "client_delivery",
                    "client_request_to_connectivity_acceptance",
                    "client_commitment",
                    "client_fyi",
                    "pm_forward",
                    "pm_handoff_to_external",
                    "ambiguous",
                ],
                "description": "Primary ownership intent — classify before proposing ball_with",
            },
            "assessment_detail": {
                "type": "object",
                "additionalProperties": False,
                "description": (
                    "Optional extraction-only payload compatible with ProjectStateAssessment. "
                    "Include it only when at least one atomic fact has an exact bounded citation. "
                    "The deterministic evaluator, not the model, owns milestone, status, ownership, "
                    "and next-action decisions. Use policy_version='1', empty milestone_criteria and "
                    "assessment_codes arrays, and null field-assessment slots. Never include hidden "
                    "reasoning or the full raw email."
                ),
                "properties": {
                    "project_id": {"type": "string", "minLength": 1},
                    "source_type": {"type": "string", "enum": ["email"]},
                    "source_ref": {"type": "string", "minLength": 1},
                    "extracted_facts": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "predicate": {
                                    "type": "string",
                                    "pattern": "^[a-z][a-z0-9_]*$",
                                },
                                "value": {"type": ["string", "boolean", "number", "null"]},
                                "subject": {"type": ["string", "null"]},
                                "citations": {
                                    "type": "array",
                                    "minItems": 1,
                                    "items": _EMAIL_ASSESSMENT_CITATION_SCHEMA,
                                },
                                "extraction_confidence": {
                                    "type": "number",
                                    "minimum": 0.0,
                                    "maximum": 1.0,
                                },
                            },
                            "required": [
                                "predicate",
                                "citations",
                                "extraction_confidence",
                            ],
                        },
                    },
                    "milestone_criteria": {"type": "array", "maxItems": 0},
                    "milestone_assessment": {"type": "null"},
                    "status_assessment": {"type": "null"},
                    "ball_with_assessment": {"type": "null"},
                    "next_action_assessment": {"type": "null"},
                    "uncertainties": {"type": "array", "items": {"type": "string"}},
                    "contradictions": {
                        "type": "array",
                        "items": _EMAIL_ASSESSMENT_CITATION_SCHEMA,
                    },
                    "confidence": _EMAIL_ASSESSMENT_CONFIDENCE_SCHEMA,
                    "policy_version": {"type": "string", "enum": ["1"]},
                    "assessment_codes": {"type": "array", "maxItems": 0},
                },
                "required": [
                    "project_id",
                    "source_type",
                    "source_ref",
                    "extracted_facts",
                    "milestone_criteria",
                    "milestone_assessment",
                    "status_assessment",
                    "ball_with_assessment",
                    "next_action_assessment",
                    "uncertainties",
                    "contradictions",
                    "confidence",
                    "policy_version",
                    "assessment_codes",
                ],
            },
        },
        "required": ["project_id", "summary", "suggested_action", "confidence", "email_intent"],
    },
    output_schema={"type": "object", "properties": {"status": {"type": "string"}}},
    allowed_task_types=["*"],
    allowed_contexts=["*"],
    side_effect_type="write",
    timeout_seconds=5,
    retry_policy="once",
    idempotency_mode="trusted_key",
)

# ─── Registry ────────────────────────────────────────────────

ALL_TOOLS: list[ToolContract] = [
    ASSISTANT_PORTFOLIO_ATTENTION,
    ASSISTANT_SEARCH_PROJECT_PARTIES,
    ASSISTANT_QUERY_PORTFOLIO,
    ASSISTANT_GET_PROJECT_BRIEF,
    ASSISTANT_SEARCH_PROJECT_TIMELINE,
    ASSISTANT_LIST_PROJECT_WORK,
    ASSISTANT_LIST_UNANSWERED_THREADS,
    ASSISTANT_GET_PROJECT_EVIDENCE_AUDIT,
    ASSISTANT_ASSESS_MILESTONE_READINESS,
    ASSISTANT_WEB_SEARCH,
    GET_PROJECT_TRUTH,
    SEARCH_EMAILS,
    GET_WORKING_MEMORY,
    GET_RELATIONSHIP_PREFS,
    DRAFT_REPLY,
    PROPOSE_TRUTH_WRITE,
    PUSH_TELEGRAM_REVIEW,
    LOG_EVIDENCE,
    THINK,
    FIND_INTERNAL_CONTACT,
    GET_PROJECT_EMAILS,
    GET_EMAIL_BODY,
    LOAD_PROJECT_CONTEXT,
    WIKI_SEARCH,
    WIKI_READ,
    WIKI_CREATE,
    WIKI_UPDATE,
    WIKI_LINK,
    LIST_REFERENCE_DOCS,
    ATTACH_REFERENCE_DOC,
    DELEGATE,
    CHAT_QUERY_PROJECTS,
    CHAT_GET_PROJECT_STATUS,
    CHAT_CREATE_PROJECT,
    CHAT_CONFIRM_CREATE_PROJECT,
    CHAT_CANCEL_CREATE_PROJECT,
    CHAT_UPDATE_PROJECT,
    CHAT_UPDATE_PROJECT_BATCH,
    CHAT_ADD_CONTACT,
    CHAT_LIST_CONTACTS,
    CHAT_ADD_NOTE,
    CHAT_MANAGE_FOLLOWUP,
    CHAT_CONFIRM_UPDATE,
    CHAT_CANCEL_UPDATE,
    CHAT_UNDO_UPDATE,
    CHAT_GET_ACTIVITY_LOG,
    CHAT_LIST_DRAFTS,
    CHAT_GET_DRAFT,
    CHAT_EDIT_DRAFT,
    CHAT_DISCARD_DRAFT,
    REPORT_EMAIL_ANALYSIS,
    AGENT_MEMORY,
    SESSION_SEARCH,
    READ_ARTIFACT,
]

# Support ticket tools (WP-10)
from src.tools.support_tools import SUPPORT_TOOL_ALIASES, SUPPORT_TOOLS  # noqa: E402
ALL_TOOLS.extend(SUPPORT_TOOLS)
ALL_TOOLS.extend(SUPPORT_TOOL_ALIASES)

from src.tools.knowledge_tools import KNOWLEDGE_TOOLS  # noqa: E402
ALL_TOOLS.extend(KNOWLEDGE_TOOLS)

from src.tools.evidence_tools import EVIDENCE_TOOLS  # noqa: E402
ALL_TOOLS.extend(EVIDENCE_TOOLS)

# Internal action tools (Sprint 2)
from src.tools.internal_action_tools import INTERNAL_ACTION_TOOLS  # noqa: E402
ALL_TOOLS.extend(INTERNAL_ACTION_TOOLS)

# Work item tools (v37)
from src.tools.work_item_tools import LIST_WORK_ITEMS_TOOL  # noqa: E402
ALL_TOOLS.append(LIST_WORK_ITEMS_TOOL)

from src.tools.communication_tools import CREATE_EMAIL_DRAFT_TOOL  # noqa: E402
ALL_TOOLS.append(CREATE_EMAIL_DRAFT_TOOL)

from src.tools.personal_work_tools import PERSONAL_WORK_TOOLS  # noqa: E402
ALL_TOOLS.extend(PERSONAL_WORK_TOOLS)

from src.tools.calendar_tools import CALENDAR_TOOLS  # noqa: E402
ALL_TOOLS.extend(CALENDAR_TOOLS)

from src.tools.fix_session_tools import FIX_SESSION_TOOLS  # noqa: E402
ALL_TOOLS.extend(FIX_SESSION_TOOLS)

from src.tools.project_reconciliation_tools import PROJECT_RECONCILIATION_TOOLS  # noqa: E402
ALL_TOOLS.extend(PROJECT_RECONCILIATION_TOOLS)

TOOL_BY_NAME: dict[str, ToolContract] = {t.name: t for t in ALL_TOOLS}


def get_tools_for_task(task_type: str) -> list[ToolContract]:
    """Return tools for a given task type, excluding LLM-invisible tools.
    In the unified loop, all tools are available — task_type filtering kept for backward compat."""
    return get_tools_for_context("lead_agent", task_type)


def get_tools_for_context(context: str, task_type: str = "*") -> list[ToolContract]:
    """Return tools available for a given context and task type.
    In the unified loop, allowed_contexts is ignored — all visible tools returned."""
    result = []
    for tool in ALL_TOOLS:
        if not tool.llm_visible:
            continue
        task_ok = "*" in tool.allowed_task_types or task_type in tool.allowed_task_types
        if task_ok:
            result.append(tool)
    return result


def get_openai_tools_schema(task_type: str = "*") -> list[dict]:
    """Return OpenAI-format tool schemas. Default returns ALL visible tools."""
    return _to_openai_schema(get_tools_for_task(task_type))


def get_all_openai_tools_schema() -> list[dict]:
    """Return OpenAI-format schemas for ALL visible tools (no filtering)."""
    return _to_openai_schema([t for t in ALL_TOOLS if t.llm_visible])


# Effects that are safe for read-only query toolset
_READ_ONLY_EFFECTS = frozenset({"read"})

_EMAIL_TOOL_NAMES = frozenset({
    "report_email_analysis",
    "manage_followup",
    "load_project_context",
    "wiki_search",
    "get_project_status",
    "think",
})

def get_orchestrator_tool_names() -> frozenset[str]:
    from src.tools.role_tools import host_tool_names
    return host_tool_names()


def get_tools_for_intent(intent: str) -> list[dict]:
    """Return OpenAI-format tool schemas filtered by conversation intent.

    intent="query"        → only tools with side_effect_type=="read"
    intent="email"        → minimal toolset for email analysis (~6 tools)
    intent="orchestrator" → host bundle with on-demand wiki/reference/ticket evidence
    intent="general"      → all visible tools (same as get_all_openai_tools_schema)

    Conservative by default: unknown intent → all tools.
    """
    if intent == "orchestrator":
        filtered = [
            t for t in ALL_TOOLS
            if t.llm_visible and t.name in get_orchestrator_tool_names()
        ]
        return _to_openai_schema(filtered)
    if intent == "query":
        filtered = [
            t for t in ALL_TOOLS
            if t.llm_visible
            and (t.side_effect_type in _READ_ONLY_EFFECTS or t.query_safe)
            # P1-5: delegate has side_effect_type="read" so it would otherwise
            # leak into the query toolset, letting a query-intent message spawn
            # a write-capable subagent. Query intent must not write.
            and t.name != "delegate"
        ]
        return _to_openai_schema(filtered)
    if intent == "email":
        filtered = [
            t for t in ALL_TOOLS
            if t.llm_visible and t.name in _EMAIL_TOOL_NAMES
        ]
        return _to_openai_schema(filtered)
    # Runtime wiki writes are curator/operator-only — not general chat agent.
    # P1-9: wiki_link also mutates the wiki graph (adds typed relations to
    # published pages) without the curator draft gate, so exclude it too.
    _AGENT_EXCLUDED = frozenset({"wiki_create", "wiki_update", "wiki_link"})
    filtered = [
        t for t in ALL_TOOLS
        if t.llm_visible and t.name not in _AGENT_EXCLUDED
    ]
    return _to_openai_schema(filtered)


def get_openai_tools_for_context(context: str, task_type: str = "*") -> list[dict]:
    """Return OpenAI-format tool schemas for a given context.
    In the unified loop, returns all visible tools regardless of context."""
    return _to_openai_schema(get_tools_for_context(context, task_type))


def get_openai_tools_by_name(names: set[str] | frozenset[str]) -> list[dict]:
    """Return OpenAI schemas for an explicit, allowlisted set of registered tools."""
    return _to_openai_schema([
        tool for tool in ALL_TOOLS if tool.llm_visible and tool.name in names
    ])


def _to_openai_schema(tools: list[ToolContract]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.input_schema,
            },
        }
        for t in tools
    ]
