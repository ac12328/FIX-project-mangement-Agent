"""
AgentFIXBPRO Configuration Module
=================================
JSON config loader with Pydantic validation.

Usage:
    from config.settings import CONFIG, BASE_DIR, get_path
"""

import os
import json
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse
from pydantic import BaseModel, Field, ValidationError
from pydantic import model_validator


BASE_DIR = Path(__file__).resolve().parent.parent
MIN_DASHBOARD_PASSWORD_LENGTH = 8


class SLAConfig(BaseModel):
    internal_days: int = 3
    external_days: int = 5
    vendor_days: int = 5
    yellow_remaining: int = 1
    silence_days: int = 7


class WriteLayerConfig(BaseModel):
    confidence_auto_threshold: float = 0.6
    auto_write_confidence_threshold: float = 0.95
    auto_write_requires_confirmation: bool = True
    # conf=1.00: apply immediately, then show undo/reject card (not pre-approve)
    auto_apply_at_perfect_confidence: bool = True
    evidence_required_fields: list[str] = Field(
        default_factory=lambda: ["current_milestone", "project_status"]
    )


class DraftWorkspaceConfig(BaseModel):
    """Release switch for the secure draft workspace surface."""

    enabled: bool = True


class AIWorkspaceConfig(BaseModel):
    """Rollout and provider controls for the portfolio AI workspace."""

    enabled: bool = True
    operator_writes_enabled: bool = False
    web_search_enabled: bool = False
    web_search_provider: str = ""
    grounding_repair_mode: Literal["legacy", "shadow", "serve"] = "legacy"
    grounding_repair_timeout_seconds: int = Field(default=20, ge=1, le=60)
    tool_centric_mode: Literal["off", "shadow", "serve"] | None = None
    tool_centric_enabled_surfaces: list[Literal["workspace", "telegram"]] = Field(
        default_factory=lambda: ["workspace", "telegram"]
    )
    tool_centric_planner_model: str = "openai/default-model"
    tool_centric_planner_timeout_seconds: int = Field(default=12, ge=1, le=120)
    tool_centric_shadow_queue_capacity: int = Field(default=256, ge=1, le=10000)

    # Legacy shadow-only keys remain accepted during the unified rollout.
    tool_centric_shadow_enabled: bool = False
    tool_centric_shadow_timeout_seconds: int = Field(default=20, ge=1, le=120)

    @model_validator(mode="after")
    def _normalize_tool_centric_surfaces(self) -> "AIWorkspaceConfig":
        self.tool_centric_enabled_surfaces = list(
            dict.fromkeys(self.tool_centric_enabled_surfaces)
        )
        self.tool_centric_planner_model = (
            self.tool_centric_planner_model.strip() or "openai/default-model"
        )
        return self


class FixSessionRegistryConfig(BaseModel):
    """Rollout and secret-display controls for the canonical FIX session registry."""

    enabled: bool = True
    auto_confirm_enabled: bool = True
    telegram_secret_reveal_enabled: bool = False


class TelegramStreamingConfig(BaseModel):
    edit_interval: float = 0.8
    buffer_threshold: int = 24
    cursor: str = " ?"
    transport: str = "auto"  # edit | draft | auto
    # HTML is the stable cross-client default. Rich Messages remain available
    # as an explicit opt-in for clients validated against Bot API 10.1+.
    format_mode: str = "html"  # rich | html | plain
    fresh_final_after_seconds: float = 0.0
    max_message_length: int = 4096
    max_rich_message_length: int = 32768
    typing_refresh_sec: float = 4.0


class TelegramThinkingConfig(BaseModel):
    """Tool progress bubbles (Scheme A) ? separate from answer streaming."""
    enabled: bool = True
    edit_interval: float = 1.5
    fast_delete_seconds: float = 2.0
    slow_keep_seconds: float = 5.0
    disable_notification: bool = True


class TelegramConfig(BaseModel):
    bot_token: str = ""
    chat_id: int = 0
    allowed_chat_ids: list[int] = Field(default_factory=list)
    allow_all_chats: bool = False
    combine_email_advisory: bool = True
    streaming: TelegramStreamingConfig = Field(default_factory=TelegramStreamingConfig)
    thinking: TelegramThinkingConfig = Field(default_factory=TelegramThinkingConfig)

    @model_validator(mode="after")
    def _normalize_allowed_chat_ids(self) -> "TelegramConfig":
        """Keep the primary chat_id compatible with the multi-chat allowlist."""
        normalized: list[int] = []
        for value in self.allowed_chat_ids:
            try:
                chat_id = int(value)
            except (TypeError, ValueError):
                continue
            if chat_id and chat_id not in normalized:
                normalized.append(chat_id)

        if self.chat_id:
            try:
                primary = int(self.chat_id)
            except (TypeError, ValueError):
                primary = 0
            if primary and primary not in normalized:
                normalized.insert(0, primary)
            self.chat_id = primary
        elif normalized:
            self.chat_id = normalized[0]

        self.allowed_chat_ids = normalized
        return self


class LLMKeysConfig(BaseModel):
    """API keys injected into os.environ at startup for LiteLLM."""
    # Active provider selection ? explicit switch instead of priority detection
    active_provider: str = ""       # openrouter|openai|anthropic|gemini|vertex_ai|minimax|ollama|custom
    active_model: str = ""          # model name within the provider
    # Backup LLM (Gemma via the local LiteLLM proxy alias by default)
    backup_model: str = "default-model"  # proxy model alias on backup endpoint
    backup_api_base: str = ""       # empty ? local_llm_api_base
    backup_api_key: str = ""        # empty ? local_llm_api_key
    # Per-provider keys
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    gemini_api_key: str = ""
    openrouter_api_key: str = ""
    minimax_api_key: str = ""       # legacy; no longer used as failover backup
    # Ollama (local daemon or cloud models via ollama pull *:cloud)
    ollama_api_base: str = "http://llm.demo.invalid/v1"
    ollama_api_key: str = ""
    # Local / Custom OpenAI-compatible endpoint
    local_llm_api_base: str = ""
    local_llm_api_key: str = ""
    local_llm_model: str = ""
    # Google Vertex AI
    vertex_ai_project: str = ""
    vertex_ai_location: str = "us-central1"
    vertex_ai_model: str = ""
    vertex_ai_credentials_path: str = ""
    vertex_ai_credentials_json: str = ""


class MsgraphConfig(BaseModel):
    enabled: bool = False
    tenant_id: str = ""
    client_id: str = ""
    client_secret: str = ""
    user_email: str = ""            # mailbox to read from / send as
    poll_folder: str = "inbox"
    poll_sent_items: bool = False   # also sync Sent Items for Outlook-sent emails
    poll_interval_seconds: int = 300
    calendar_enabled: bool = False
    calendar_id: str = "calendar"
    calendar_sync_interval_seconds: int = 300
    calendar_past_days: int = 7
    calendar_future_days: int = 30


class ImapConfig(BaseModel):
    enabled: bool = False
    append_sent: bool = True            # append to Sent Items after SMTP send (independent of polling)
    host: str = ""
    port: int = 993
    username: str = ""
    password: str = ""
    use_ssl: bool = True
    inbox_folder: str = "INBOX"
    sent_folder: str = "Sent Items"   # Exchange default
    poll_interval_seconds: int = 300
    sent_window_hours: int = 24       # how far back to scan Sent Items


class EmailConfig(BaseModel):
    backend: str = "smtp"             # smtp | console | msgraph
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_tls: bool = True
    from_email: str = ""
    from_name: str = ""
    self_bcc: bool = False            # BCC sender ? ensures copy lands in POP3 inbox (Outlook workaround)
    sender_identities: list[dict] = Field(default_factory=list)
    imap: ImapConfig = Field(default_factory=ImapConfig)


class WorkingHoursConfig(BaseModel):
    enabled: bool = True
    weekdays_only: bool = True
    start_hour: int = 8       # HH ? inclusive (08:00)
    end_hour: int = 20        # HH ? exclusive (before 20:00)
    timezone: str = "Asia/Hong_Kong"


class ThresholdsConfig(BaseModel):
    """Configurable thresholds for proactive automation features."""
    stale_external_days: int = 5
    proactive_cooldown_days: int = 7
    draft_expiry_days: int = 14
    commitment_followup_days: int = 5
    resolution_learning_delay_minutes: int = 30
    resolution_reply_timeout_minutes: int = 1440


class AgentHarnessConfig(BaseModel):
    """Plan A: precise next-step + optional rule-58 draft in chat."""
    auto_draft_on_rule58: bool = False
    require_wiki_on_progress_advisory: bool = True


class ProjectStateConfig(BaseModel):
    """Conservative rollout controls for evidence-grounded project state."""

    evidence_grounded_enabled: bool = True
    shadow_legacy_inference: bool = True
    require_assessment_for_milestone_review: bool = True
    contract_version: str = Field(default="2", pattern=r"^[12]$")


class MemoryReformConfig(BaseModel):
    """Memory harness reform flags (Phases 0?4). Default off until PROT enablement."""
    operator_memory_enabled: bool = False
    memory_guard_enabled: bool = False
    thread_artifacts_enabled: bool = False
    memory_flush_to_store: bool = False


class LearningGovernanceConfig(BaseModel):
    """Independent rollout and emergency controls for Learning Governance v2."""
    enabled: bool = False
    capture_enabled: bool = False
    outbox_v2_enabled: bool = False
    dual_write: bool = False
    legacy_direct_enabled: bool = False
    extractor_dry_run: bool = False
    candidate_only: bool = False
    governance_enforced: bool = False
    evaluation_required: bool = False
    project_learned_rules_projection: bool = False
    response_pattern_projection: bool = False
    operator_memory_projection: bool = False
    private_evaluation_corpus: str = ""
    private_evaluation_enabled: bool = False
    retriever_shadow: bool = False
    retrieval_v2_chat: bool = False
    retrieval_v2_web: bool = False
    retrieval_v2_draft: bool = False
    retrieval_v2_advisory: bool = False
    retrieval_v2_tool: bool = False
    wiki_publication_v2: bool = False
    writes_disabled: bool = False
    worker_disabled: bool = False
    retrieval_disabled: bool = False
    global_memory_disabled: bool = False
    auto_activation_disabled: bool = False
    wiki_publication_disabled: bool = False
    feedback_reinforcement_disabled: bool = False
    exact_outcome_attribution_v2: bool = False
    lifecycle_v2_enabled: bool = False
    lifecycle_kill_switch: bool = False


class PersonalAssistantConfig(BaseModel):
    """Feature flags for the personal work assistant expansion."""

    enabled: bool = False
    universal_email_enabled: bool = False
    work_items_enabled: bool = False
    reminders_enabled: bool = False
    personal_brief_enabled: bool = False
    inbox_intelligence_enabled: bool = False
    calendar_read_enabled: bool = False
    meeting_prep_enabled: bool = False
    meeting_ingestion_enabled: bool = False
    calendar_write_enabled: bool = False


class EmailProjectTruthConfig(BaseModel):
    """Rollout controls for the email Project Truth B path."""

    mode: Literal["off", "shadow", "serve"] = "off"
    shadow_queue_capacity: int = Field(default=128, ge=1, le=10_000)
    shadow_timeout_seconds: int = Field(default=20, ge=1, le=120)
    shadow_model: str = ""


class ObservabilityConfig(BaseModel):
    """Structured trace capture, diagnosis, and retention controls."""

    enabled: bool = True
    full_payload_capture: bool = False
    auto_diagnosis: bool = True
    payload_retention_days: int = 30
    detail_retention_days: int = 180
    queue_capacity: int = 10_000
    retry_interval_seconds: float = 0.2
    shutdown_flush_seconds: float = 5.0
    spool_path: str | None = None

    @model_validator(mode="after")
    def _validate_payload_encryption(self) -> "ObservabilityConfig":
        if self.full_payload_capture:
            from src.observability.payload_crypto import validate_encryption_key
            validate_encryption_key()
        return self


class DashboardAuthConfig(BaseModel):
    """Dashboard authentication, registration, and session settings."""

    enabled: bool = True
    mode: Literal["local", "entra"] = "local"
    local_registration_enabled: bool = False
    allowed_email_domains: list[str] = Field(default_factory=lambda: ["demo.invalid"])
    email_verification_ttl_minutes: int = 30
    password_reset_ttl_minutes: int = 30
    password_min_length: int = MIN_DASHBOARD_PASSWORD_LENGTH
    tenant_id: str = ""
    client_id: str = ""
    client_secret: str = ""
    redirect_uri: str = "http://127.0.0.1:8317/api/auth/entra/callback"
    app_role: str = "Dashboard.Access"
    initial_admin_oid: str = ""
    initial_admin_email: str = ""
    absolute_session_hours: int = 12
    idle_session_minutes: int = 120
    session_touch_minutes: int = 5
    oidc_transaction_secret: str = ""
    cookie_secure: bool = False
    production: bool = False
    legacy_password_login_enabled: bool = False
    # Legacy fields are read only when the explicit compatibility flag is on.
    username: str = "operator"
    password_hash: str = ""
    session_secret: str = ""
    session_ttl_minutes: int = 480


class AppConfig(BaseModel):
    database_file: str = "data/public-demo.db"
    data_dir: str = "data"
    api_port: int = 8317  # isolated demo API; runtime startup is blocked
    dashboard_base_url: str = ""  # e.g. "http://127.0.0.1:3317" ? enables deep links in card footers
    support_routing_email: str = ""  # internal address for ticket routing notifications
    sla: SLAConfig = Field(default_factory=SLAConfig)
    write_layer: WriteLayerConfig = Field(default_factory=WriteLayerConfig)
    draft_workspace: DraftWorkspaceConfig = Field(default_factory=DraftWorkspaceConfig)
    ai_workspace: AIWorkspaceConfig = Field(default_factory=AIWorkspaceConfig)
    fix_session_registry: FixSessionRegistryConfig = Field(default_factory=FixSessionRegistryConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    llm_keys: LLMKeysConfig = Field(default_factory=LLMKeysConfig)
    msgraph: MsgraphConfig = Field(default_factory=MsgraphConfig)
    email: EmailConfig = Field(default_factory=EmailConfig)
    email_project_truth: EmailProjectTruthConfig = Field(
        default_factory=EmailProjectTruthConfig
    )
    working_hours: WorkingHoursConfig = Field(default_factory=WorkingHoursConfig)
    thresholds: ThresholdsConfig = Field(default_factory=ThresholdsConfig)
    agent_harness: AgentHarnessConfig = Field(default_factory=AgentHarnessConfig)
    project_state: ProjectStateConfig = Field(default_factory=ProjectStateConfig)
    operator_id: str = "default_operator"
    memory_reform: MemoryReformConfig = Field(default_factory=MemoryReformConfig)
    learning_governance: LearningGovernanceConfig = Field(default_factory=LearningGovernanceConfig)
    personal_assistant: PersonalAssistantConfig = Field(default_factory=PersonalAssistantConfig)
    dashboard_auth: DashboardAuthConfig = Field(default_factory=DashboardAuthConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    work_hours_start: str = "08:30"
    work_hours_end: str = "18:00"

    company_domains: set[str] = {
        "demo.invalid", "provisioning.demo.invalid",
        "acceptance.demo.invalid", "exception.demo.invalid",
    }
    preview_before_send: bool = True
    commitment_requires_confirmation: bool = True
    wiki_curator_auto_publish: bool = False
    escalation_contacts: list[int] = Field(default_factory=list)  # Telegram chat_ids to escalate to
    escalation_after_hours: int = 24  # hours after first SLA alert before escalation triggers
    orchestrator_mode: bool = True  # thin host + delegate specialists (default on)
    email_specialist_mode: bool = True  # lean email analysis harness
    unified_loop_v2: bool = True  # golden loop rebuild (E8 enabled)


def apply_runtime_flags(config: "AppConfig") -> None:
    """Sync config.json feature flags into config.defaults (import-time module)."""
    from config import defaults

    defaults.DRAFT_WORKSPACE_ENABLED = bool(config.draft_workspace.enabled)
    defaults.AI_WORKSPACE_ENABLED = bool(config.ai_workspace.enabled)
    defaults.WORKSPACE_OPERATOR_WRITES_ENABLED = bool(
        config.ai_workspace.operator_writes_enabled
    )
    defaults.AI_WORKSPACE_WEB_SEARCH_ENABLED = bool(config.ai_workspace.web_search_enabled)
    defaults.AI_WORKSPACE_WEB_SEARCH_PROVIDER = config.ai_workspace.web_search_provider.strip().lower()
    defaults.AI_WORKSPACE_GROUNDING_REPAIR_MODE = config.ai_workspace.grounding_repair_mode
    defaults.AI_WORKSPACE_GROUNDING_REPAIR_TIMEOUT_SECONDS = int(
        config.ai_workspace.grounding_repair_timeout_seconds
    )
    workspace = config.ai_workspace
    explicit_fields = workspace.model_fields_set
    effective_tool_centric_mode = workspace.tool_centric_mode
    if effective_tool_centric_mode is None:
        effective_tool_centric_mode = (
            "shadow" if workspace.tool_centric_shadow_enabled else "off"
        )
    effective_tool_centric_timeout = int(workspace.tool_centric_planner_timeout_seconds)
    if (
        "tool_centric_planner_timeout_seconds" not in explicit_fields
        and "tool_centric_shadow_timeout_seconds" in explicit_fields
    ):
        effective_tool_centric_timeout = int(workspace.tool_centric_shadow_timeout_seconds)

    defaults.TOOL_CENTRIC_MODE = effective_tool_centric_mode
    defaults.TOOL_CENTRIC_ENABLED_SURFACES = frozenset(
        workspace.tool_centric_enabled_surfaces
    )
    defaults.TOOL_CENTRIC_PLANNER_MODEL = workspace.tool_centric_planner_model
    defaults.TOOL_CENTRIC_PLANNER_TIMEOUT_SECONDS = effective_tool_centric_timeout
    defaults.TOOL_CENTRIC_SHADOW_QUEUE_CAPACITY = int(
        workspace.tool_centric_shadow_queue_capacity
    )

    # Synchronize legacy aliases so the existing shadow worker and health API
    # continue to behave correctly during migration.
    defaults.AI_WORKSPACE_TOOL_CENTRIC_SHADOW_ENABLED = (
        effective_tool_centric_mode == "shadow"
        and "workspace" in defaults.TOOL_CENTRIC_ENABLED_SURFACES
    )
    defaults.AI_WORKSPACE_TOOL_CENTRIC_SHADOW_QUEUE_CAPACITY = int(
        workspace.tool_centric_shadow_queue_capacity
    )
    defaults.AI_WORKSPACE_TOOL_CENTRIC_SHADOW_TIMEOUT_SECONDS = (
        effective_tool_centric_timeout
    )
    defaults.ORCHESTRATOR_MODE = bool(config.orchestrator_mode)
    defaults.EMAIL_SPECIALIST_MODE = bool(config.email_specialist_mode)
    email_project_truth = config.email_project_truth
    defaults.EMAIL_PROJECT_TRUTH_MODE = email_project_truth.mode
    defaults.EMAIL_PROJECT_TRUTH_SHADOW_QUEUE_CAPACITY = int(
        email_project_truth.shadow_queue_capacity
    )
    defaults.EMAIL_PROJECT_TRUTH_SHADOW_TIMEOUT_SECONDS = int(
        email_project_truth.shadow_timeout_seconds
    )
    defaults.EMAIL_PROJECT_TRUTH_SHADOW_MODEL = email_project_truth.shadow_model.strip()
    defaults.OPERATOR_ID = config.operator_id or "default_operator"
    fix_sessions = config.fix_session_registry
    defaults.FIX_SESSION_REGISTRY_ENABLED = bool(fix_sessions.enabled)
    defaults.FIX_SESSION_AUTO_CONFIRM_ENABLED = bool(fix_sessions.auto_confirm_enabled)
    defaults.FIX_SESSION_SECRET_REVEAL_ENABLED = bool(fix_sessions.telegram_secret_reveal_enabled)
    mr = config.memory_reform
    defaults.OPERATOR_MEMORY_ENABLED = bool(mr.operator_memory_enabled)
    defaults.MEMORY_GUARD_ENABLED = bool(mr.memory_guard_enabled)
    defaults.THREAD_ARTIFACTS_ENABLED = bool(mr.thread_artifacts_enabled)
    defaults.MEMORY_FLUSH_TO_STORE = bool(mr.memory_flush_to_store)

    governance = config.learning_governance
    defaults.LEARNING_V2_ENABLED = bool(governance.enabled)
    defaults.LEARNING_CAPTURE_ENABLED = bool(governance.capture_enabled)
    defaults.LEARNING_OUTBOX_V2_ENABLED = bool(governance.outbox_v2_enabled)
    defaults.LEARNING_DUAL_WRITE = bool(governance.dual_write)
    defaults.LEARNING_LEGACY_DIRECT_ENABLED = bool(governance.legacy_direct_enabled)
    defaults.LEARNING_EXTRACTOR_DRY_RUN = bool(governance.extractor_dry_run)
    defaults.LEARNING_CANDIDATE_ONLY = bool(governance.candidate_only)
    defaults.LEARNING_GOVERNANCE_ENFORCED = bool(governance.governance_enforced)
    defaults.LEARNING_EVALUATION_REQUIRED = bool(governance.evaluation_required)
    defaults.LEARNING_PROJECT_LEARNED_RULES_PROJECTION = bool(governance.project_learned_rules_projection)
    defaults.LEARNING_RESPONSE_PATTERN_PROJECTION = bool(governance.response_pattern_projection)
    defaults.LEARNING_OPERATOR_MEMORY_PROJECTION = bool(governance.operator_memory_projection)
    defaults.MEMORY_RETRIEVER_SHADOW = bool(governance.retriever_shadow)
    defaults.MEMORY_RETRIEVAL_V2_CHAT = bool(governance.retrieval_v2_chat)
    defaults.MEMORY_RETRIEVAL_V2_WEB = bool(governance.retrieval_v2_web)
    defaults.MEMORY_RETRIEVAL_V2_DRAFT = bool(governance.retrieval_v2_draft)
    defaults.MEMORY_RETRIEVAL_V2_ADVISORY = bool(governance.retrieval_v2_advisory)
    defaults.MEMORY_RETRIEVAL_V2_TOOL = bool(governance.retrieval_v2_tool)
    defaults.WIKI_PUBLICATION_V2 = bool(governance.wiki_publication_v2)
    defaults.LEARNING_WRITES_DISABLED = bool(governance.writes_disabled)
    defaults.LEARNING_WORKER_DISABLED = bool(governance.worker_disabled)
    defaults.MEMORY_RETRIEVAL_DISABLED = bool(governance.retrieval_disabled)
    defaults.GLOBAL_MEMORY_DISABLED = bool(governance.global_memory_disabled)
    defaults.AUTO_ACTIVATION_DISABLED = bool(governance.auto_activation_disabled)
    defaults.WIKI_PUBLICATION_DISABLED = bool(governance.wiki_publication_disabled)
    defaults.FEEDBACK_REINFORCEMENT_DISABLED = bool(governance.feedback_reinforcement_disabled)
    defaults.EXACT_OUTCOME_ATTRIBUTION_V2 = bool(governance.exact_outcome_attribution_v2)
    defaults.MEMORY_LIFECYCLE_V2_ENABLED = bool(governance.lifecycle_v2_enabled)
    defaults.MEMORY_LIFECYCLE_KILL_SWITCH = bool(governance.lifecycle_kill_switch)

    pa = config.personal_assistant
    defaults.PERSONAL_ASSISTANT_ENABLED = bool(pa.enabled)
    defaults.PERSONAL_ASSISTANT_UNIVERSAL_EMAIL_ENABLED = bool(
        pa.enabled and pa.universal_email_enabled
    )
    defaults.PERSONAL_ASSISTANT_WORK_ITEMS_ENABLED = bool(
        pa.enabled and pa.work_items_enabled
    )
    defaults.PERSONAL_ASSISTANT_REMINDERS_ENABLED = bool(
        pa.enabled and pa.reminders_enabled
    )
    defaults.PERSONAL_ASSISTANT_PERSONAL_BRIEF_ENABLED = bool(
        pa.enabled and pa.personal_brief_enabled
    )
    defaults.PERSONAL_ASSISTANT_INBOX_INTELLIGENCE_ENABLED = bool(
        pa.inbox_intelligence_enabled
    )
    defaults.PERSONAL_ASSISTANT_CALENDAR_READ_ENABLED = bool(
        pa.enabled and pa.calendar_read_enabled
    )
    defaults.PERSONAL_ASSISTANT_MEETING_PREP_ENABLED = bool(
        pa.enabled and pa.meeting_prep_enabled
    )
    defaults.PERSONAL_ASSISTANT_MEETING_INGESTION_ENABLED = bool(
        pa.enabled and pa.meeting_ingestion_enabled
    )
    defaults.PERSONAL_ASSISTANT_CALENDAR_WRITE_ENABLED = bool(
        pa.enabled and pa.calendar_write_enabled
    )
    defaults.UNIFIED_LOOP_V2 = bool(config.unified_loop_v2)
    state = config.project_state
    defaults.PROJECT_STATE_EVIDENCE_GROUNDED_ENABLED = bool(
        state.evidence_grounded_enabled
    )
    defaults.PROJECT_STATE_SHADOW_LEGACY_INFERENCE = bool(
        state.shadow_legacy_inference
    )
    defaults.PROJECT_STATE_REQUIRE_ASSESSMENT_FOR_MILESTONE_REVIEW = bool(
        state.require_assessment_for_milestone_review
    )
    defaults.PROJECT_STATE_CONTRACT_VERSION = state.contract_version


def inject_llm_keys(config: "AppConfig") -> None:
    """Inject LLM API keys from config into os.environ for LiteLLM.
    Also patches the routing table for the active provider."""
    k = config.llm_keys
    env_mapping = {
        "OPENAI_API_KEY":     k.openai_api_key,
        "ANTHROPIC_API_KEY":  k.anthropic_api_key,
        "GEMINI_API_KEY":     k.gemini_api_key,
        "OPENROUTER_API_KEY": k.openrouter_api_key,
        "MINIMAX_API_KEY":    k.minimax_api_key,
        "VERTEXAI_PROJECT":   k.vertex_ai_project,
        "VERTEXAI_LOCATION":  k.vertex_ai_location,
        "VERTEXAI_MODEL":     k.vertex_ai_model,
    }
    for env_var, value in env_mapping.items():
        if value and not os.environ.get(env_var):
            os.environ[env_var] = value

    if k.vertex_ai_credentials_json and not os.environ.get("VERTEXAI_CREDENTIALS"):
        os.environ["VERTEXAI_CREDENTIALS"] = k.vertex_ai_credentials_json

    if k.vertex_ai_credentials_path and not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(get_path(k.vertex_ai_credentials_path))

    # New path: explicit active_provider overrides legacy detection
    if k.active_provider and k.active_model:
        from src.llm.routing import configure_provider
        _api_key_for = {
            "openrouter": k.openrouter_api_key,
            "openai": k.openai_api_key,
            "anthropic": k.anthropic_api_key,
            "gemini": k.gemini_api_key,
            "minimax": k.minimax_api_key,
            "ollama": k.ollama_api_key or "",
            "custom": k.local_llm_api_key,
        }
        _api_base_for = {
            "custom": k.local_llm_api_base or None,
            "ollama": k.ollama_api_base or "http://llm.demo.invalid/v1",
        }
        configure_provider(
            provider=k.active_provider,
            model=k.active_model,
            api_key=_api_key_for.get(k.active_provider) or None,
            api_base=_api_base_for.get(k.active_provider),
        )
        _apply_vision_routing(k)
        return

    # Legacy fallback: detect provider from non-empty fields
    if k.local_llm_api_base:
        from src.llm.routing import configure_local_llm
        configure_local_llm(
            api_base=k.local_llm_api_base,
            api_key=k.local_llm_api_key,
            model=k.local_llm_model or None,
        )
        _apply_vision_routing(k)
    elif k.vertex_ai_project:
        from src.llm.routing import configure_vertex_ai
        configure_vertex_ai(model=k.vertex_ai_model or None)
        _apply_vision_routing(k)


def _apply_vision_routing(k: LLMKeysConfig) -> None:
    """Text models (e.g. glm-5.2:cloud) cannot describe email screenshots."""
    from src.llm.routing import configure_vision_routing

    if k.local_llm_api_base:
        configure_vision_routing(
            api_base=k.local_llm_api_base,
            api_key=k.local_llm_api_key,
            model=k.local_llm_model or "default-model",
        )
        return
    configure_vision_routing(
        api_base=k.ollama_api_base or "http://llm.demo.invalid/v1",
        api_key=k.ollama_api_key or "",
        model="kimi-k2.6:cloud",
    )


def _apply_secret_env_overrides(config: AppConfig) -> AppConfig:
    """Override persisted credentials with deployment-managed secrets.

    Environment presence, including an explicit empty value, takes precedence
    over persisted credentials. The documented MS Graph variable wins when both
    it and the backwards-compatible alias are present.
    """
    overrides = (
        (("AGENTFIX_DEMO_TELEGRAM_BOT_TOKEN",), config.telegram, "bot_token"),
        (("AGENTFIX_DEMO_TELEGRAM_CHAT_ID",), config.telegram, "chat_id"),
        (("AGENTFIX_DEMO_SMTP_PASSWORD",), config.email, "smtp_password"),
        (("AGENTFIX_DEMO_IMAP_PASSWORD",), config.email.imap, "password"),
        (
            ("AGENTFIX_DEMO_MSGRAPH_CLIENT_SECRET", "AGENTFIX_DEMO_GRAPH_CLIENT_SECRET"),
            config.msgraph,
            "client_secret",
        ),
        (
            ("AGENTFIX_DEMO_DASHBOARD_ENTRA_CLIENT_SECRET",),
            config.dashboard_auth,
            "client_secret",
        ),
    )
    for env_names, target, field_name in overrides:
        for env_name in env_names:
            if env_name in os.environ:
                value: str | int = os.environ[env_name]
                if target is config.telegram and field_name == "chat_id":
                    try:
                        value = int(value)
                    except (TypeError, ValueError):
                        value = 0
                setattr(target, field_name, value)
                break
    return config


_CONFIG_LOAD_ERROR: str | None = None


def _summarize_config_load_error(exc: Exception) -> str:
    """Return a credential-safe startup error without serializing config values."""
    if isinstance(exc, json.JSONDecodeError):
        return (
            "config.json contains invalid JSON "
            f"at line {exc.lineno}, column {exc.colno}"
        )
    if isinstance(exc, ValidationError):
        details: list[str] = []
        for error in exc.errors():
            location = ".".join(str(part) for part in error.get("loc", ())) or "config"
            message = str(error.get("msg") or "validation failed")
            details.append(f"{location}: {message}")
            if len(details) >= 5:
                break
        suffix = "; ".join(details) or "validation failed"
        return f"config.json validation failed: {suffix}"
    return f"config.json could not be loaded ({type(exc).__name__})"


def _load_config() -> AppConfig:
    """Load config while retaining a startup-fatal, credential-safe error."""
    global _CONFIG_LOAD_ERROR
    _CONFIG_LOAD_ERROR = None
    config_path = BASE_DIR / "config.json"
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8-sig") as f:
                raw = json.load(f)
            return _apply_secret_env_overrides(AppConfig(**raw))
        except Exception as exc:
            # Import-time fallback keeps tooling and tests usable. Production
            # startup calls validate_config(include_load_error=True) and exits
            # before any service, LLM, database, or trace worker is started.
            _CONFIG_LOAD_ERROR = _summarize_config_load_error(exc)
    return _apply_secret_env_overrides(AppConfig())


CONFIG = _load_config()
apply_runtime_flags(CONFIG)


def require_valid_config_load() -> None:
    """Fail service startup when config.json could not be validated safely."""
    if _CONFIG_LOAD_ERROR:
        raise RuntimeError(_CONFIG_LOAD_ERROR)


def get_path(relative: str) -> Path:
    """Resolve a relative path against BASE_DIR."""
    p = Path(relative)
    if p.is_absolute():
        return p
    return BASE_DIR / p


DB_PATH = get_path(CONFIG.database_file)
DATA_DIR = get_path(CONFIG.data_dir)


def validate_config(*, include_load_error: bool = False) -> list[str]:
    """Check essential config fields and optional startup-fatal load errors."""
    errors: list[str] = []
    if include_load_error and _CONFIG_LOAD_ERROR:
        return [_CONFIG_LOAD_ERROR]
    if not CONFIG.telegram.bot_token:
        errors.append("telegram.bot_token is missing")
    if not CONFIG.telegram.chat_id:
        errors.append("telegram.chat_id is missing")

    # At least one LLM key must be set
    k = CONFIG.llm_keys
    has_llm = any([
        k.openai_api_key, k.anthropic_api_key, k.gemini_api_key,
        k.openrouter_api_key, k.minimax_api_key,
        k.local_llm_api_base, k.vertex_ai_project,
    ])
    if not has_llm:
        errors.append("No LLM API key configured (need at least one of: openai, anthropic, gemini, openrouter, local_llm, vertex_ai)")

    if CONFIG.email.imap.enabled:
        imap = CONFIG.email.imap
        if not imap.host:
            errors.append("email.imap.host is missing (IMAP enabled but no host)")
        if not imap.username or not imap.password:
            errors.append("email.imap.username/password missing (IMAP enabled)")

    if CONFIG.msgraph.enabled:
        mg = CONFIG.msgraph
        for field in ("tenant_id", "client_id", "client_secret", "user_email"):
            if not getattr(mg, field):
                errors.append(f"msgraph.{field} is missing (Graph API enabled)")

    if CONFIG.dashboard_auth.enabled:
        auth = CONFIG.dashboard_auth
        if auth.mode == "entra":
            for field in ("tenant_id", "client_id", "client_secret", "redirect_uri", "app_role"):
                if not getattr(auth, field):
                    errors.append(
                        f"dashboard_auth.{field} is missing (Microsoft Entra sign-in enabled)"
                    )
            if not auth.initial_admin_oid and not auth.initial_admin_email:
                errors.append(
                    "dashboard_auth.initial_admin_oid or initial_admin_email is required "
                    "to bind the primary SSO Admin"
                )
            if len(auth.oidc_transaction_secret) < 32:
                errors.append(
                    "dashboard_auth.oidc_transaction_secret must be at least 32 characters"
                )
        if auth.mode == "local":
            domains = [domain.strip().casefold().lstrip("@") for domain in auth.allowed_email_domains]
            if not domains or any(not domain or "." not in domain for domain in domains):
                errors.append("dashboard_auth.allowed_email_domains must contain valid domains")
            if auth.password_min_length < MIN_DASHBOARD_PASSWORD_LENGTH:
                errors.append(
                    f"dashboard_auth.password_min_length must be at least "
                    f"{MIN_DASHBOARD_PASSWORD_LENGTH}"
                )
            if auth.email_verification_ttl_minutes < 5:
                errors.append("dashboard_auth.email_verification_ttl_minutes must be at least 5")
            if auth.password_reset_ttl_minutes < 5:
                errors.append("dashboard_auth.password_reset_ttl_minutes must be at least 5")
        if auth.absolute_session_hours < 1:
            errors.append("dashboard_auth.absolute_session_hours must be at least 1")
        if auth.idle_session_minutes < 5:
            errors.append("dashboard_auth.idle_session_minutes must be at least 5")
        if auth.session_touch_minutes < 1:
            errors.append("dashboard_auth.session_touch_minutes must be at least 1")
        if auth.production and not auth.cookie_secure:
            errors.append("dashboard_auth.cookie_secure must be true in production")
        if auth.production:
            dashboard_url = urlparse(CONFIG.dashboard_base_url.strip())
            if dashboard_url.scheme != "https" or not dashboard_url.netloc:
                errors.append("dashboard_base_url must be an absolute HTTPS URL in production")
        if auth.legacy_password_login_enabled:
            if not auth.password_hash.startswith("pbkdf2_sha256$"):
                errors.append(
                    "dashboard_auth.password_hash must be a PBKDF2-SHA256 hash "
                    "when legacy password login is enabled"
                )
            if len(auth.session_secret) < 32:
                errors.append(
                    "dashboard_auth.session_secret must be at least 32 characters "
                    "when legacy password login is enabled"
                )

    return errors
