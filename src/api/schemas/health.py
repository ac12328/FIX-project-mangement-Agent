from pydantic import BaseModel


class LLMProviderStatus(BaseModel):
    name: str
    reachable: bool
    latency_ms: int | None = None
    model: str


class HealthResponse(BaseModel):
    status: str  # "ok", "degraded", or "llm_unavailable"
    uptime_seconds: float
    db_ok: bool
    llm_ok: bool
    schema_version: int
    active_projects: int
    last_bot_heartbeat: str | None


class LLMHealthResponse(BaseModel):
    primary: LLMProviderStatus
    backup: LLMProviderStatus
    active_provider: str  # "primary" | "backup" | "none"

class ToolStatusSummary(BaseModel):
    status: str
    tracked_tool_count: int
    open_circuit_count: int
    half_open_circuit_count: int
    degraded_tool_count: int
    exposure_count: int
    policy_required_count: int
    selection_count: int
    authorization_allowed_count: int
    selection_blocked_count: int
    duplicate_selection_count: int
    execution_count: int
    model_execution_count: int
    runtime_success_count: int
    infrastructure_failure_count: int
    receipt_success_count: int
    receipt_failure_count: int
    receipt_denied_count: int
    circuit_blocked_count: int
    authorization_rate: float | None
    duplicate_selection_rate: float | None
    model_selection_execution_rate: float | None
    runtime_success_rate: float | None
    outcome_success_rate: float | None


class ToolRuntimeStatus(BaseModel):
    tool_name: str
    side_effect_type: str
    state: str
    circuit_eligible: bool
    exposure_count: int
    policy_required_count: int
    selection_count: int
    authorization_allowed_count: int
    selection_blocked_count: int
    duplicate_selection_count: int
    execution_count: int
    model_execution_count: int
    runtime_success_count: int
    infrastructure_failure_count: int
    receipt_success_count: int
    receipt_failure_count: int
    receipt_denied_count: int
    circuit_blocked_count: int
    consecutive_infrastructure_failures: int
    total_circuit_trips: int
    last_latency_ms: float | None
    last_runtime_error_code: str | None
    last_runtime_error: str | None
    last_outcome_error_code: str | None
    last_selection_block_reason: str | None
    last_execution_at: str | None
    circuit_open_until: str | None
    retry_after_seconds: float
    selection_rate: float | None
    authorization_rate: float | None
    duplicate_selection_rate: float | None
    runtime_success_rate: float | None
    outcome_success_rate: float | None


class ToolStatusResponse(BaseModel):
    generated_at: str
    summary: ToolStatusSummary
    tools: list[ToolRuntimeStatus]
