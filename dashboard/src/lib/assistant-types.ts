import type { components } from "@/lib/generated/api-schema";

export type AssistantScopeMode = "portfolio" | "projects";
export type AssistantRole = components["schemas"]["AssistantMessageResponse"]["role"];

export type AssistantCommandCapability = components["schemas"]["AssistantCommandCapability"];
export type AssistantCapabilities = components["schemas"]["AssistantCapabilitiesResponse"];

export type AssistantThread = components["schemas"]["AssistantThreadResponse"];

export interface AssistantTableColumn {
  key: string;
  label: string;
}

export interface AssistantTextPart {
  type: "text";
  text: string;
}

export interface AssistantTablePart {
  type: "table";
  title?: string;
  columns: AssistantTableColumn[];
  rows: Array<Record<string, unknown>>;
  export_filename?: string;
}

export interface AssistantSourcePart {
  type: "source";
  source_id: string;
  title: string;
  url?: string | null;
  snippet?: string;
  kind?: string;
  locator?: string;
  project_id?: string | null;
  occurred_at?: string | null;
  metadata?: Record<string, unknown>;
}

export interface AssistantToolResultPart {
  type: "tool_result";
  tool: string;
  status?: string;
  text?: string;
  scope_mode?: AssistantScopeMode;
  project_ids?: string[];
  filters?: Record<string, unknown>;
}

export interface AssistantDraftPart {
  type: "draft";
  draft_id: string;
  project_id?: string | null;
  subject: string;
  body: string;
  to_email?: string | null;
  status: string;
  url: string;
}

export interface AssistantActionDiff {
  field: string;
  before?: unknown;
  after?: unknown;
}

export interface AssistantActionPart {
  type: "action";
  action_id: string;
  action_type: string;
  status: string;
  risk: string;
  target_type: string;
  target_id: string;
  diff: AssistantActionDiff[];
  expires_at?: string | null;
  approve_url: string;
  reject_url: string;
}

export interface AssistantFollowUpQuestionsPart {
  type: "follow_up_questions";
  questions: string[];
}

export interface AssistantErrorPart {
  type: "error";
  code?: string;
  message: string;
}

export type OperatorUpdatePartStatus =
  | "needs_intake"
  | "pending"
  | "executing"
  | "confirmed"
  | "cancelled"
  | "failed"
  | "expired"
  | "superseded";

export interface OperatorUpdateCriterion {
  id: string;
  description?: string;
  result?: string;
}

export interface AssistantOperatorUpdatePart {
  type: "operator_update";
  status: OperatorUpdatePartStatus | string;
  project_id: string;
  operation_id?: string;
  kind?: "operator_override" | "evaluator_aligned" | string;
  fields?: Record<string, string>;
  current_values?: Record<string, unknown>;
  missing_criteria?: OperatorUpdateCriterion[];
  reason?: string;
  evidence_ref?: string | null;
  transition?: string;
  base_version?: string | null;
  expires_at?: string | null;
  outcome?: string;
  error?: string;
  resolved_at?: string | null;
  high_risk?: boolean;
}

export type AssistantPart =
  | AssistantTextPart
  | AssistantTablePart
  | AssistantSourcePart
  | AssistantToolResultPart
  | AssistantDraftPart
  | AssistantActionPart
  | AssistantFollowUpQuestionsPart
  | AssistantErrorPart
  | AssistantOperatorUpdatePart
  | ({ type: string } & Record<string, unknown>);

// HTTP envelope is generated; presentation narrows the open-ended part payloads.
export type AssistantMessage = Omit<components["schemas"]["AssistantMessageResponse"], "parts"> & {
  parts: AssistantPart[];
};
export type AssistantMessagePage = Omit<components["schemas"]["AssistantMessagePage"], "items"> & {
  items: AssistantMessage[];
};

export interface AssistantIntent {
  type: "status" | "compare" | "draft" | "aggregate_query" | "project_update";
  project_ids?: string[];
  prompt?: string;
  field?: string;
  value?: unknown;
}

export interface AssistantTurnPayload {
  client_turn_id: string;
  message: string;
  command?: string;
  intent?: AssistantIntent;
}

export type AssistantStreamEvent =
  | { event: "turn.started"; data: { turn_id: string; replayed?: boolean } }
  | { event: "progress"; data: { turn_id: string; phase: string; message: string } }
  | { event: "text.delta"; data: { turn_id: string; delta: string } }
  | { event: "part.added"; data: { turn_id: string; part: AssistantPart } }
  | { event: "turn.completed"; data: { turn_id: string; message_id?: string | null; replayed?: boolean } }
  | { event: "turn.failed"; data: { turn_id: string; error: string } };

export interface AssistantThreadCreate {
  title?: string;
  scope_mode?: AssistantScopeMode;
  project_ids?: string[];
  web_enabled?: boolean;
}

export interface AssistantThreadPatch {
  title?: string;
  scope_mode?: AssistantScopeMode;
  project_ids?: string[];
  web_enabled?: boolean;
  archived?: boolean;
}
