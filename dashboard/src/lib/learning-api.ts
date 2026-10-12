import { ApiError, fetchJson } from "./api";

export type LearningPage<T = Record<string, unknown>> = { items: T[]; total: number; limit: number; offset: number };
export type LearningAction = {
  action: string; reason: string; idempotency_key: string; confirmed?: boolean;
  confirmation_token?: string; expected_revision?: string; extractor_version?: string;
  candidate_id?: string; candidate_version?: number; merge_candidate_id?: string;
  merge_candidate_version?: number; replacement_candidate_id?: string;
  replacement_candidate_version?: number; operation_id?: string; proposed_value?: unknown;
  scope_type?: string; scope_id?: string; suggested_expiry?: string;
};
export type LearningActionResult = { success: boolean; action: string; event_type: string; status?: string; candidate_id?: string; candidate_version?: number; fact_id?: string; decision_id?: string; conflict_id?: string; job_id?: string; operation_id?: string; effective?: boolean };
export type LearningCandidate = {
  candidate_id: string;
  candidate_version: number;
  operator_id: string;
  target_type: string;
  subject_type?: string | null;
  subject_id?: string | null;
  predicate: string;
  proposed_value: unknown;
  scope_type: string;
  scope_id?: string | null;
  confidence: number;
  suggested_expiry?: string | null;
  reasoning_summary?: string | null;
  evidence_refs?: unknown[];
  disposition?: string | null;
  risk_level?: string | null;
  validation_id?: string | null;
  passed?: boolean | number | null;
  created_at: string;
};
export type LearningRecord = Record<string, unknown>;
export type LearningCandidateDetail = {
  candidate: LearningRecord & Pick<LearningCandidate, "candidate_id" | "candidate_version" | "target_type" | "predicate" | "proposed_value" | "scope_type" | "confidence">;
  observation: LearningRecord | null;
  evidence: LearningRecord[];
  validation: LearningRecord | null;
  evaluation: LearningRecord | null;
  decision: LearningRecord | null;
  decisions: LearningRecord[];
  fact: LearningRecord | null;
  fact_versions: LearningRecord[];
  exposures: LearningRecord[];
  outcomes: LearningRecord[];
  conflicts: LearningRecord[];
  publications: LearningRecord[];
  lifecycle: LearningRecord[];
};
export type LearningCandidateFilters = {
  status?: string;
  risk_level?: string;
  limit?: number;
  offset?: number;
};
export type LearningFact = { fact_id: string; fact_version: number; candidate_id: string; candidate_version: number; target_type: string; predicate: string; value_json: string; scope_type: string; scope_id?: string | null; status: string; valid_from: string; valid_to?: string | null };
export type LearningConflict = { conflict_id: string; canonical_key: string; status: string; risk_level: string; authority_blocked: number; updated_at: string };
export type LearningJob = { job_id: string; observation_id: string; job_type: string; extractor_version: string; state: string; attempts: number; available_at: string; last_error?: string | null; updated_at: string };
export type LearningAudit = { id: number; actor: string; action_type: string; object_ref?: string | null; summary: string; metadata_json: string; created_at: string };

const stableJson = (value: Record<string, unknown>) => JSON.stringify(value, Object.keys(value).sort());
const sha256 = async (value: string) => {
  const bytes = new TextEncoder().encode(value);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
};
export const confirmation = async (action: string, resource: string, revision: string, binding?: {
  resource_version: number; scope: string; risk: string; actor_id: string; reason: string;
}) => {
  if (!binding) return { confirmed: true, expected_revision: revision, confirmation_token: `${action}:${resource}:${revision}` };
  const payload = stableJson({ action, actor_id: binding.actor_id, expected_revision: revision, reason: binding.reason,
    resource_id: resource, resource_version: binding.resource_version, risk: binding.risk, scope: binding.scope });
  return { confirmed: true, expected_revision: revision, confirmation_token: `lcf_${await sha256(payload)}` };
};
export const candidateRevision = (item: LearningCandidate) => `${item.candidate_id}:${item.candidate_version}:${item.validation_id}:${item.disposition}`;
export const factRevision = (item: LearningFact) => `${item.fact_id}:${item.fact_version}:${item.status}`;
export const conflictRevision = (item: LearningConflict) => `${item.conflict_id}:${item.updated_at}`;
export const jobRevision = (item: LearningJob) => `${item.job_id}:${item.state}:${item.attempts}:${item.extractor_version}:${item.updated_at}`;

export const getLearningCandidates = (filters: LearningCandidateFilters = {}) => {
  const query = new URLSearchParams();
  if (filters.status) query.set("status", filters.status);
  if (filters.risk_level) query.set("risk_level", filters.risk_level);
  if (filters.limit !== undefined) query.set("limit", String(filters.limit));
  if (filters.offset !== undefined) query.set("offset", String(filters.offset));
  const suffix = query.size ? `?${query.toString()}` : "";
  return fetchJson<LearningPage<LearningCandidate>>(`/api/learning-center/candidates${suffix}`);
};
export const getLearningCandidate = (id: string, version: number) => fetchJson<LearningCandidateDetail>(`/api/learning-center/candidates/${encodeURIComponent(id)}/${version}`);
export const actOnLearningCandidate = (id: string, version: number, body: LearningAction) => fetchJson<LearningActionResult>(`/api/learning-center/candidates/${encodeURIComponent(id)}/${version}/actions`, { method: "POST", body: JSON.stringify(body) });
export const getLearningFacts = () => fetchJson<LearningPage<LearningFact>>("/api/learning-center/facts");
export const getLearningFact = (id: string) => fetchJson<Record<string, unknown>>(`/api/learning-center/facts/${encodeURIComponent(id)}`);
export const actOnLearningFact = (id: string, body: LearningAction) => fetchJson<LearningActionResult>(`/api/learning-center/facts/${encodeURIComponent(id)}/actions`, { method: "POST", body: JSON.stringify(body) });
export const getLearningConflicts = () => fetchJson<LearningPage<LearningConflict>>("/api/learning-center/conflicts");
export const actOnLearningConflict = (id: string, body: LearningAction) => fetchJson<LearningActionResult>(`/api/learning-center/conflicts/${encodeURIComponent(id)}/actions`, { method: "POST", body: JSON.stringify(body) });
export const getLearningJobs = () => fetchJson<LearningPage<LearningJob>>("/api/learning-center/jobs");
export const actOnLearningJob = (id: string, body: LearningAction) => fetchJson<LearningActionResult>(`/api/learning-center/jobs/${encodeURIComponent(id)}/actions`, { method: "POST", body: JSON.stringify(body) });
export const getLearningAudit = () => fetchJson<LearningPage<LearningAudit>>("/api/learning-center/audit");
export const getLearningRollbacks = () => fetchJson<LearningPage>("/api/learning-center/rollbacks");
export const getLearningKillSwitches = () => fetchJson<{ items: Array<{ name: string; effective: boolean }> }>("/api/learning-center/kill-switches");
export const setLearningKillSwitch = (name: string, value: boolean, reason: string, current: boolean) => {
  const revision = `${name}:${String(current).toLowerCase()}`;
  return confirmation("set_kill_switch", name, revision).then((confirmed) =>
    fetchJson<LearningActionResult>(`/api/learning-center/kill-switches/${encodeURIComponent(name)}`, { method: "POST", body: JSON.stringify({ value, reason, idempotency_key: crypto.randomUUID(), ...confirmed }) })
  );
};
export function learningErrorState(error: unknown): "forbidden" | "stale" | "error" { if (error instanceof ApiError && error.status === 403) return "forbidden"; if (error instanceof ApiError && error.status === 409) return "stale"; return "error"; }
