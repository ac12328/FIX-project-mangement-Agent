import { fetchJson, LLM_TIMEOUT_MS } from "./api";

export interface ProjectBriefingEvent {
  source_ref: string;
  occurred_at: string;
  actor_name: string;
  actor_role: "internal" | "external" | string;
  event_type: string;
  summary: string;
}

export interface ProjectSummaryAI {
  current_state: string;
  blocker: string;
  recommended_action: string;
  next_action: string;
  has_blocker: boolean;
  ball_with: "Internal" | "External" | "Shared" | "Unknown" | string;
  events: ProjectBriefingEvent[];
  generated_at: string;
  evidence_cutoff_at: string | null;
  source_count: number;
}

export const getProjectSummary = (projectId: string, force = false) => {
  const qs = force ? "?force=true" : "";
  return fetchJson<ProjectSummaryAI>(
    `/api/projects/${projectId}/summary${qs}`,
    { timeoutMs: LLM_TIMEOUT_MS },
  );
};
