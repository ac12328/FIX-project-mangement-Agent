// TypeScript interfaces mirroring FastAPI response schemas

export interface BriefItem {
  project_id: string;
  client_name: string | null;
  current_milestone: string | null;
  ball_with: string | null;
  next_action: string | null;
  urgency: string;
  urgency_reasons: string[];
  days_inactive: number | null;
  pending_drafts: number;
  open_followups: number;
}

export interface DashboardData {
  generated_at: string;
  total_active: number;
  red_items: BriefItem[];
  yellow_items: BriefItem[];
  green_items: BriefItem[];
  pending_reviews_count: number;
  pending_drafts_count: number;
  open_followups_count: number;
  overdue_followups_count: number;
  due_in_3_days_count: number;
}

export interface ProjectSummary {
  project_id: string;
  client_name: string | null;
  client_email: string | null;
  market: string | null;
  project_status: string;
  ball_with: string;
  current_milestone: string | null;
  current_milestone_display: string | null;
  target_go_live: string | null;
  next_action: string | null;
  updated_at: string | null;
  last_activity_at: string | null;
  operator_id?: string;
}

export interface ProjectDetail extends ProjectSummary {
  broker_code: string | null;
  connection_type: string | null;
  notes: string | null;
  logbook: string | null;
  sla_limit: number | null;
  operator_id: string;
  created_at: string | null;
}

export interface Draft {
  draft_id: string;
  project_id: string | null;
  task_type: string | null;
  status: string;
  subject: string | null;
  current_draft_text: string | null;
  draft_version: number;
  to_email: string | null;
  cc: string | null;
  ticket_id: string | null;
  created_at: string | null;
  thread_subject: string | null;
  thread_sender: string | null;
  thread_cc: string | null;
}

export interface ReviewItem {
  update_id: string;
  project_id: string;
  proposed_changes: string;
  task_type: string | null;
  confidence: number;
  risk_level: string | null;
  created_at: string | null;
  client_name: string | null;
  assessment_json: string | null;
  policy_version: string | null;
  source_type: string | null;
  source_ref: string | null;
}

export type ReviewChange = {
  field: string;
  old_value?: string;
  new_value: string;
  reason?: string;
  evidence_ref?: string;
};

export type ReviewCitation = {
  quote?: string;
  source_ref?: string;
  source_type?: string;
};

export type ReviewCriterion = {
  criterion_id?: string;
  result?: "met" | "missing" | "contradicted" | "not_applicable";
  explanation?: string;
  citations?: ReviewCitation[];
};

export type ReviewAssessment = {
  milestone_criteria?: ReviewCriterion[];
  milestone_assessment?: ReviewFieldAssessment;
  status_assessment?: ReviewFieldAssessment;
  ball_with_assessment?: ReviewFieldAssessment;
  next_action_assessment?: ReviewFieldAssessment;
  uncertainties?: string[];
  contradictions?: ReviewCitation[];
  confidence?: Record<string, number>;
};

export type ReviewFieldAssessment = {
  decision?: string;
  risk_level?: string;
  reasons?: string[];
  citations?: ReviewCitation[];
};

export interface ActionResult {
  success: boolean;
  message: string;
  action_id?: string;
}

export interface ActivityEntry {
  id: number;
  project_id: string | null;
  actor: string;
  action_type: string;
  summary: string | null;
  surface: string | null;
  created_at: string;
}

export interface EvidenceItem {
  evidence_id: string;
  project_id: string;
  evidence_type: string;
  content_summary: string;
  content_text?: string;
  source_ref?: string;
  evidence_at: string;
}

export interface MemoryItem {
  project_id: string;
  memory_key: string;
  memory_value: string;
  updated_at: string;
}

export interface AggregateQueryDateRange {
  from_date: string | null;
  to_date: string | null;
}

export interface AggregateQueryScope {
  project_filter: string | null;
  date_range: AggregateQueryDateRange;
  contact_filter: string | null;
}

export interface AggregateQueryProfile {
  task_type: string;
  query_text: string;
  generated_at: string;
  scope: AggregateQueryScope;
  fields_requested: string[];
  sort_order: string | null;
  limit: number | null;
  confidence: number;
  escalate_to_operator: boolean;
  escalation_reason: string | null;
}

export interface AggregateQueryRow {
  project_id: string;
  client_name: string | null;
  market: string | null;
  project_status: string | null;
  ball_with: string | null;
  current_milestone: string | null;
  current_milestone_display: string | null;
  next_action: string | null;
}

export interface AggregateQueryResponse {
  status: string;
  clarification_question: string | null;
  profile: AggregateQueryProfile;
  suggested_filters: Record<string, string>;
  filters_applied: Record<string, string | boolean>;
  total: number;
  results: AggregateQueryRow[];
}

export interface TraceEvent {
  event_id: string;
  run_id: string;
  sequence: number;
  event_type: string;
  round_index: number | null;
  llm_trace_id: string | null;
  tool_name: string | null;
  status: string;
  duration_ms: number | null;
  payload_available: boolean;
  created_at: string;
}

export interface TraceDiagnostic {
  diagnostic_id: string;
  run_id: string;
  trigger: string;
  status: string;
  severity: string | null;
  root_cause: string | null;
  summary: string | null;
  created_at: string;
  completed_at: string | null;
}

export interface TraceRun {
  run_id: string;
  surface: string;
  task: string;
  project_id: string | null;
  status: string;
  exit_reason: string | null;
  started_at: string;
  ended_at: string | null;
  duration_ms: number | null;
  implicit: boolean;
}

export interface TraceRunDetail extends TraceRun {
  events: TraceEvent[];
  diagnostics: TraceDiagnostic[];
  llm_traces: TraceSummary[];
}

export interface TraceSummary {
  trace_id: string;
  task: string;
  tokens_in: number;
  tokens_out: number;
  latency_ms: number;
  output_valid: number | boolean;
  error_class: string | null;
  decision_outcome: string | null;
  prompt_hash: string | null;
  run_id: string | null;
  created_at: string;
}

export type TraceDetail = TraceSummary;

export interface ClosingIssue {
  code: string;
  message: string;
  count: number | null;
  requires_acknowledgement: boolean;
}

export interface ClosingItem {
  item_id: string;
  item_key: string;
  label: string;
  status: "pending" | "confirmed" | "dismissed";
  can_dismiss: boolean;
  dismissed_reason: string | null;
  confirmed_at: string | null;
  evidence_ref: string | null;
  resolution_note: string | null;
  resolved_by: string | null;
  resolved_at: string | null;
  initiated_at: string | null;
}

export interface ClosingStatusResponse {
  project_id: string;
  project_status: string;
  current_milestone: string | null;
  project_updated_at: string | null;
  target_go_live: string | null;
  next_action: string | null;
  items: ClosingItem[];
  initiated: boolean;
  initiated_at: string | null;
  signed_off_by: string | null;
  signed_off_at: string | null;
  sign_off_note: string | null;
  acknowledgements: string[];
  closed_at: string | null;
  can_initiate: boolean;
  can_close: boolean;
  can_manage: boolean;
  blockers: ClosingIssue[];
  warnings: ClosingIssue[];
  close_blocked_reasons: string[];
  checklist: Record<string, boolean>;
}

export interface OperatorPreference {
  id: number | null;
  scope: "project" | "counterparty";
  project_id: string | null;
  counterparty_id: string | null;
  tone: string;
  language: string;
  push_style: string;
  custom_notes: string | null;
  updated_at: string | null;
}

export interface ProjectSnapshot {
  project_id: string;
  client_name: string | null;
  current_milestone: string | null;
  ball_with: string;
  project_status: string;
  days_since_last_activity: number | null;
}

export interface ReportData {
  report_type: string;
  period_start: string;
  period_end: string;
  snapshots: ProjectSnapshot[];
  highlights: string[];
  milestone_distribution: Record<string, number>;
  markdown: string;
}

export interface IntakePayload {
  project_id?: string;
  client_name?: string;
  client_email?: string;
  market?: string;
  connection_type?: string;
  target_go_live?: string;
  free_text?: string;
  confirm?: boolean;
  source?: string;
}

export interface IntakeResult {
  project_id: string;
  client_name: string;
  client_email: string | null;
  market: string | null;
  connection_type: string;
  target_go_live: string | null;
  notes: string | null;
  source: string;
  confidence: number;
  needs_confirmation: boolean;
  extraction_warnings: string[];
  committed: boolean;
  message: string;
}

export interface PortfolioProjectHealth {
  project_id: string;
  client_name: string;
  current_milestone: string;
  ball_with: string;
  project_tier: string | null;
  days_stale: number;
  health: "green" | "yellow" | "red";
  pending_count: number;
}

export interface PortfolioHealthData {
  projects: PortfolioProjectHealth[];
  summary: { green: number; yellow: number; red: number };
}

export interface ConversationMessage {
  direction: "inbound" | "outbound";
  text: string;
  message_kind: string;
  created_at: string;
}

export interface ProgressProject {
  last_action: string;
  state: string;
  touched_at: string;
}

export interface AgentProgress {
  version: number;
  updated_at: string;
  projects: Record<string, ProgressProject>;
  recent_decisions: string[];
  pending: string[];
  session_notes: string;
}

export interface ConversationContext {
  chat_id: number;
  summary: string | null;
  summary_updated_at: string | null;
  recent_messages: ConversationMessage[];
  progress: AgentProgress | null;
}

export interface ContextLayerResponse {
  name: string;
  label: string;
  content: string;
  token_estimate: number;
}

export interface ContextPreviewResponse {
  layers: ContextLayerResponse[];
  intent: string;
  detected_project_id: string | null;
  available_tools: string[];
  missing_flags: string[];
  context_confidence: number;
  total_token_estimate: number;
  skills_block: string;
  planning_hint: boolean;
}

export interface OverdueFollowup {
  followup_id: string;
  project_id: string;
  client_name: string | null;
  description: string;
  due_date: string;
  days_overdue: number;
}

export interface ComingUpItem {
  project_id: string;
  client_name: string | null;
  label: string;
  due_date: string;
  days_until: number;
}

export interface ComingUpData {
  today: ComingUpItem[];
  tomorrow: ComingUpItem[];
  day2: ComingUpItem[];
}

export interface Attachment {
  filename: string;
  size: number;
  path: string;
}

export interface ProjectContact {
  contact_id: string;
  display_name: string;
  primary_email: string;
  role: string | null;
  department: string | null;
  confirmed: boolean;
}

export interface FixSessionEndpoint {
  route_role: string;
  host: string;
  port: number;
  priority: number;
  transport: string;
}

export interface FixSessionRecord {
  session_version_id: string;
  project_id: string;
  environment: "UAT" | "PRODUCTION" | string;
  session_key: string;
  version_no: number;
  is_current: number | boolean;
  begin_string: string | null;
  sender_comp_id: string | null;
  target_comp_id: string | null;
  heartbeat_seconds: number | null;
  reset_seq_num_on_logon: number | boolean | null;
  account_reference: string | null;
  confirmation_mode: string;
  confirmed_by: string | null;
  confirmed_at: string | null;
  superseded_at: string | null;
  source_candidate_id: string | null;
  metadata: Record<string, unknown>;
  endpoints: FixSessionEndpoint[];
  has_username_secret: boolean;
  has_password_secret: boolean;
}

export interface FixSessionHistoryRecord {
  session_version_id: string;
  project_id: string;
  environment: string;
  session_key: string;
  version_no: number;
  is_current: number | boolean;
  begin_string: string | null;
  sender_comp_id: string | null;
  target_comp_id: string | null;
  heartbeat_seconds: number | null;
  reset_seq_num_on_logon: number | boolean | null;
  account_reference: string | null;
  confirmation_mode: string;
  confirmed_by: string | null;
  confirmed_at: string | null;
  superseded_at: string | null;
  source_candidate_id: string | null;
}

export interface FixSessionCandidate {
  candidate_id: string;
  project_id: string;
  environment: string;
  session_key: string;
  sender_email: string | null;
  confidence: number;
  completeness: number;
  conflict_reasons: string[];
  changed_fields: string[];
  inherited_fields: string[];
  source_email_id: string | null;
  received_at: string | null;
  created_at: string;
}

export interface FixSessionDecisionResult {
  candidate_id: string;
  status: "confirmed" | "rejected";
  session_version_id?: string;
}
