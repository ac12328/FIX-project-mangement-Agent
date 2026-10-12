"""
Default constants for AgentFIXBPRO.
Enum values, thresholds, SLA constants.
"""

# Schema version
CURRENT_SCHEMA_VERSION = 143

# Team Product v1: second-person gate for high-risk writes/sends.
# Production default is on. Product tests opt in explicitly; conftest leaves
# the suite on the historical single-operator path unless a test enables it.
TEAM_FOUR_EYES_ENABLED = True

CLAIM_TTL_SECONDS = 15 * 60


# Draft workspace release switch. Safety services and the durable outbox remain active
# for legacy compatibility paths even when the workspace surface is rolled back.
DRAFT_WORKSPACE_ENABLED = True

# AI Workspace rollout controls. Web search remains opt-in and unavailable
# unless a supported provider is explicitly configured.
AI_WORKSPACE_ENABLED = True
WORKSPACE_OPERATOR_WRITES_ENABLED = False
AI_WORKSPACE_WEB_SEARCH_ENABLED = False
AI_WORKSPACE_WEB_SEARCH_PROVIDER = ""
AI_WORKSPACE_GROUNDING_REPAIR_MODE = "legacy"
AI_WORKSPACE_GROUNDING_REPAIR_TIMEOUT_SECONDS = 20

# Shared purpose-sized read rollout. Workspace and Telegram use the same router,
# typed query tool, renderer, and fail-open fallback contract.
TOOL_CENTRIC_MODE = "off"  # off | shadow | serve
TOOL_CENTRIC_ENABLED_SURFACES = frozenset({"workspace", "telegram"})
TOOL_CENTRIC_PLANNER_MODEL = "openai/default-model"
TOOL_CENTRIC_PLANNER_TIMEOUT_SECONDS = 12
TOOL_CENTRIC_SHADOW_QUEUE_CAPACITY = 256

# Email Project Truth B rollout mode. Serve still routes writes through existing guards.
EMAIL_PROJECT_TRUTH_MODE = "off"  # off | shadow | serve
EMAIL_PROJECT_TRUTH_SHADOW_QUEUE_CAPACITY = 128
EMAIL_PROJECT_TRUTH_SHADOW_TIMEOUT_SECONDS = 20
EMAIL_PROJECT_TRUTH_SHADOW_MODEL = ""

# Legacy Workspace-shadow aliases retained for config and health compatibility.
AI_WORKSPACE_TOOL_CENTRIC_SHADOW_ENABLED = False
AI_WORKSPACE_TOOL_CENTRIC_SHADOW_QUEUE_CAPACITY = 256
AI_WORKSPACE_TOOL_CENTRIC_SHADOW_TIMEOUT_SECONDS = 20


# Canonical FIX session registry rollout controls.
FIX_SESSION_REGISTRY_ENABLED = True
FIX_SESSION_AUTO_CONFIRM_ENABLED = True
FIX_SESSION_SECRET_REVEAL_ENABLED = False

# Memory reform feature flags (synced from config.json via apply_runtime_flags)
OPERATOR_MEMORY_ENABLED = False
MEMORY_GUARD_ENABLED = False
THREAD_ARTIFACTS_ENABLED = False
MEMORY_FLUSH_TO_STORE = False
OPERATOR_ID = "default_operator"

# Learning Governance v2 rollout and emergency controls (all default off).
LEARNING_V2_ENABLED = False
LEARNING_CAPTURE_ENABLED = False
LEARNING_OUTBOX_V2_ENABLED = False
LEARNING_DUAL_WRITE = False
LEARNING_LEGACY_DIRECT_ENABLED = False
LEARNING_EXTRACTOR_DRY_RUN = False
LEARNING_CANDIDATE_ONLY = False
LEARNING_GOVERNANCE_ENFORCED = False
LEARNING_EVALUATION_REQUIRED = False
LEARNING_PROJECT_LEARNED_RULES_PROJECTION = False
LEARNING_RESPONSE_PATTERN_PROJECTION = False
LEARNING_OPERATOR_MEMORY_PROJECTION = False
MEMORY_RETRIEVER_SHADOW = False
MEMORY_RETRIEVAL_V2_CHAT = False
MEMORY_RETRIEVAL_V2_WEB = False
MEMORY_RETRIEVAL_V2_DRAFT = False
MEMORY_RETRIEVAL_V2_ADVISORY = False
MEMORY_RETRIEVAL_V2_TOOL = False
WIKI_PUBLICATION_V2 = False
LEARNING_WRITES_DISABLED = False
LEARNING_WORKER_DISABLED = False
MEMORY_RETRIEVAL_DISABLED = False
GLOBAL_MEMORY_DISABLED = False
AUTO_ACTIVATION_DISABLED = False
WIKI_PUBLICATION_DISABLED = False
FEEDBACK_REINFORCEMENT_DISABLED = False
EXACT_OUTCOME_ATTRIBUTION_V2 = False
MEMORY_LIFECYCLE_V2_ENABLED = False
MEMORY_LIFECYCLE_KILL_SWITCH = False

# Personal assistant feature flags (synced from config.json via apply_runtime_flags)
PERSONAL_ASSISTANT_ENABLED = False
PERSONAL_ASSISTANT_UNIVERSAL_EMAIL_ENABLED = False
PERSONAL_ASSISTANT_WORK_ITEMS_ENABLED = False
PERSONAL_ASSISTANT_REMINDERS_ENABLED = False
PERSONAL_ASSISTANT_PERSONAL_BRIEF_ENABLED = False
PERSONAL_ASSISTANT_INBOX_INTELLIGENCE_ENABLED = False
PERSONAL_ASSISTANT_CALENDAR_READ_ENABLED = False
PERSONAL_ASSISTANT_MEETING_PREP_ENABLED = False
PERSONAL_ASSISTANT_MEETING_INGESTION_ENABLED = False
PERSONAL_ASSISTANT_CALENDAR_WRITE_ENABLED = False

# Unified loop v2 rebuild (synced from config.json via apply_runtime_flags)
UNIFIED_LOOP_V2 = True

# Evidence-grounded project-state rollout flags
PROJECT_STATE_EVIDENCE_GROUNDED_ENABLED = True
PROJECT_STATE_SHADOW_LEGACY_INFERENCE = True
PROJECT_STATE_REQUIRE_ASSESSMENT_FOR_MILESTONE_REVIEW = True
PROJECT_STATE_CONTRACT_VERSION = "2"

# Independently authored demo vendor profile; not a renamed vendor roster.
# Vendor domains must not become client_email routing shortcuts.
VENDOR_EMAIL_DOMAINS = frozenset({"vendor.demo.invalid"})
VENDOR_ORG_DISPLAY_NAMES: dict[str, str] = {
    "vendor.demo.invalid": "Demo Vendor",
}
VENDOR_DOMAIN_ALIASES: dict[str, tuple[str, ...]] = {}

# Confidence thresholds
CONFIDENCE_AUTO_THRESHOLD = 0.6  # Below this → requires_operator_review
CONFIDENCE_HIGH = 0.85
AUTO_WRITE_CONFIDENCE_THRESHOLD = 0.95  # Email auto-write requires this minimum

# SLA defaults (business days)
SLA_INTERNAL_DAYS = 3
SLA_EXTERNAL_DAYS = 5
SLA_VENDOR_DAYS = 5

# Working memory staleness (days)
WORKING_MEMORY_STALENESS_DAYS = 7

# Tool loop safety cap — model decides when to stop, this is the maximum
MAX_TOOL_LOOP_ROUNDS = 10

# Writable project truth fields — the ONLY fields the Write Layer can update
WRITABLE_PROJECT_FIELDS = frozenset({
    "project_status",
    "current_milestone",
    "ball_with",
    "next_action",
    "notes",
    "target_go_live",
    "broker_code",
    "connection_type",
    "sla_limit",
    "client_name",
    "client_email",
    "market",
    "project_tier",
})

# High-impact fields that require evidence (Guard 3)
EVIDENCE_REQUIRED_FIELDS = frozenset({
    "project_status",
})

# Communication task types that must NOT write truth (Guard 6)
COMMUNICATION_TASK_TYPES = frozenset({
    "reply_to_client",
    "draft_follow_up",
    "answer_status",
})

# Project statuses considered "active" — whitelist for dashboard queries
ACTIVE_PROJECT_STATUSES = ("Active", "Blocked", "Waiting")

# Notification budget defaults (operator_state)
DEFAULT_MAX_DAILY_NOTIFICATIONS = 15
DEFAULT_NOTIFICATION_DELIVERY_MODE = "realtime"

# Configurable thresholds (defaults — override via config.json "thresholds" section)
STALE_EXTERNAL_DAYS = 5
PROACTIVE_COOLDOWN_DAYS = 7
DRAFT_EXPIRY_DAYS = 14
COMMITMENT_DEFAULT_FOLLOWUP_DAYS = 5

# Orchestrator mode — thin host + delegate → specialist loops (default on)
ORCHESTRATOR_MODE = True

# Email specialist — lean run_agent_loop for [INBOUND EMAIL/REPLY]
EMAIL_SPECIALIST_MODE = True

# LLM context window — shared with src/llm/client.py (default-model = 96K)
MODEL_CONTEXT_WINDOW = 96_000

# Application content budgets (aligned to MODEL_CONTEXT_WINDOW)
KNOWLEDGE_CHAR_BUDGET = 20_000
WIKI_CHUNK_CHARS = 3_000
AGENT_NOTES_CHAR_LIMIT = 15_000
EMAIL_BODY_SNIPPET_CHARS = 4_000
EMAIL_BODY_STORE_CHARS = 8_000
EMAIL_BODY_ANALYSIS_CHARS = 12_000
TOOL_EMAIL_PAGE_CHARS = 4_000
TOOL_WIKI_KNOWLEDGE_CHARS = 6_000
# Retrieval re-rank / pack (PR2)
WIKI_HUB_SCORE_BOOST = 0.20
WIKI_DOMAIN_SOFT_BOOST = 0.10
WIKI_TYPE_PREF_BOOST = 0.12
WIKI_PLAYBOOK_ATTACH_CHARS = 4_000
WIKI_SEARCH_OVERFETCH = 4  # chunks per requested hit before path de-dupe
CONVERSATION_HISTORY_LIMIT = 20
COMPRESS_MESSAGE_THRESHOLD = 40
COMPRESS_TAIL_KEEP = 15
COMPRESS_SUMMARY_THRESHOLD = 35
EVIDENCE_DISPLAY_LIMIT = 20

# Wiki curator token budget (aligned to MODEL_CONTEXT_WINDOW)
WIKI_CURATOR_OUTPUT_RESERVE = 6_000       # reserved for LLM JSON output
WIKI_CURATOR_SAFETY_MARGIN = 500          # schema augmentation + estimation slack
WIKI_CURATOR_TOKEN_BUDGET = (
    MODEL_CONTEXT_WINDOW - WIKI_CURATOR_OUTPUT_RESERVE - WIKI_CURATOR_SAFETY_MARGIN
)
WIKI_CURATOR_MAX_JOB_SECONDS = 600        # 10 min hard timeout for the entire job
WIKI_CURATOR_INDEX_TOKEN_LIMIT = 10_000   # compress wiki index if exceeds this
WIKI_CURATOR_MAX_ITEMS_PER_RUN = 80       # scheduled run cap — drains backlog gradually
WIKI_CURATOR_LLM_TIMEOUT_SEC = 120        # per-batch LLM timeout (default watchdog is 90s)
WIKI_COMPILER_BATCH_SIZE = 12             # nuggets per nightly compile batch
