import type {
  DashboardData,
  ProjectSummary,
  ProjectDetail,
  Draft,
  ReviewItem,
  ActionResult,
  ActivityEntry,
  EvidenceItem,
  MemoryItem,
  AggregateQueryResponse,
  TraceSummary,
  TraceDetail,
  TraceRun,
  TraceRunDetail,
  TraceDiagnostic,
  ClosingStatusResponse,
  OperatorPreference,
  ReportData,
  IntakePayload,
  IntakeResult,
  PortfolioHealthData,
  OverdueFollowup,
  ComingUpData,
  Attachment,
} from "./types";

import { API_BASE as BASE } from "./api-base";
import type { ChatHistoryResponse, ChatMessage, ChatSendResponse } from "./chat-types";
const REQUEST_TIMEOUT_MS = 8_000;
export const LLM_TIMEOUT_MS = 60_000;
const SEND_TIMEOUT_MS = 90_000;

export interface RuntimeCapabilities {
  draft_workspace: {
    enabled: boolean;
  };
  personal_assistant: {
    enabled: boolean;
    capabilities: Record<string, boolean>;
  };
}

export const getRuntimeCapabilities = () =>
  fetchJson<RuntimeCapabilities>("/api/capabilities");

type ApiErrorPayload = {
  error?: boolean;
  message?: string;
  detail?: string | unknown;
};

export class ApiError extends Error {
  status: number | null;
  detail: unknown;
  errorCode: string | null;

  constructor(
    message: string,
    status: number | null = null,
    detail: unknown = null,
    errorCode: string | null = null,
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    this.errorCode = errorCode;
  }
}

async function readResponseBody(res: Response): Promise<unknown> {
  const text = await res.text();
  if (!text) {
    return null;
  }

  try {
    return JSON.parse(text) as ApiErrorPayload;
  } catch {
    return text;
  }
}

export async function fetchJson<T>(path: string, init?: RequestInit & { timeoutMs?: number }): Promise<T> {
  const controller = new AbortController();
  let didTimeout = false;
  const timeout = init?.timeoutMs ?? REQUEST_TIMEOUT_MS;
  const timeoutId = window.setTimeout(() => {
    didTimeout = true;
    controller.abort();
  }, timeout);

  const forwardAbort = () => controller.abort();
  init?.signal?.addEventListener("abort", forwardAbort, { once: true });

  try {
    if (init?.signal?.aborted) {
      throw new ApiError("The request was cancelled.");
    }
    const method = (init?.method ?? "GET").toUpperCase();
    const csrfToken = typeof document !== "undefined"
      ? document.cookie
          .split("; ")
          .find((entry) => entry.startsWith("agentfixb_csrf="))
          ?.split("=")
          .slice(1)
          .join("=")
      : undefined;
    const headers = new Headers(init?.headers);
    if (!(init?.body instanceof FormData)) {
      headers.set("Content-Type", "application/json");
    }
    if (csrfToken && !["GET", "HEAD", "OPTIONS"].includes(method)) {
      headers.set("X-CSRF-Token", decodeURIComponent(csrfToken));
    }
    const res = await fetch(`${BASE}${path}`, {
      ...init,
      credentials: "include",
      signal: controller.signal,
      headers,
    });

    if (!res.ok) {
      const publicAuthPaths = new Set([
        "/api/auth/config",
        "/api/auth/session",
        "/api/auth/login",
        "/api/auth/register",
        "/api/auth/verify-email",
        "/api/auth/resend-verification",
        "/api/auth/forgot-password",
        "/api/auth/reset-password",
        "/api/auth/emergency/login",
      ]);
      const publicAuthRequest = publicAuthPaths.has(path) || path.startsWith("/api/auth/entra/");
      const publicPages = new Set([
        "/login",
        "/register",
        "/verify-email",
        "/forgot-password",
        "/reset-password",
        "/emergency-login",
      ]);
      const publicPage = typeof window !== "undefined" && publicPages.has(window.location.pathname);
      if (res.status === 401 && !publicAuthRequest && !publicPage && typeof window !== "undefined") {
        const redirectTo = `${window.location.pathname}${window.location.search}`;
        window.location.assign(`/login?redirect_to=${encodeURIComponent(redirectTo)}`);
      }
      const payload = (await readResponseBody(res)) as ApiErrorPayload | string | null;
      const detail = typeof payload === "object" && payload !== null
        ? (payload as ApiErrorPayload).detail
        : payload;
      let errorCode: string | null = null;
      if (detail && typeof detail === "object" && !Array.isArray(detail) && "code" in detail) {
        const code = (detail as { code?: unknown }).code;
        if (typeof code === "string") errorCode = code;
      }
      const nestedMessage = detail && typeof detail === "object" && !Array.isArray(detail)
        ? String((detail as { message?: unknown }).message || "").trim()
        : "";
      const errText =
        typeof payload === "object" && payload !== null
          ? ((payload as ApiErrorPayload).message?.trim() ||
             nestedMessage ||
             (typeof (payload as ApiErrorPayload).detail === "string"
               ? ((payload as ApiErrorPayload).detail as string).trim()
               : Array.isArray((payload as ApiErrorPayload).detail)
                 ? ((payload as ApiErrorPayload).detail as Array<{ msg?: string }>)
                     .map(e => e?.msg ?? JSON.stringify(e))
                     .join("; ")
                 : undefined))
          : typeof payload === "string" ? payload.trim() : null;
      const message = errText || "Could not load data.";

      throw new ApiError(
        message,
        res.status,
        typeof payload === "object" && payload !== null ? payload.detail : payload,
        errorCode,
      );
    }

    return res.json() as Promise<T>;
  } catch (error) {
    if (error instanceof ApiError) {
      throw error;
    }
    if (didTimeout) {
      throw new ApiError(
        `Could not load data. The request timed out after ${timeout / 1000} seconds.`
      );
    }
    if (error instanceof Error && error.message) {
      throw new ApiError(error.message);
    }
    throw new ApiError("Could not load data.");
  } finally {
    window.clearTimeout(timeoutId);
    init?.signal?.removeEventListener("abort", forwardAbort);
  }
}


export interface SseEventMessage {
  event: string;
  data: string;
  id?: string;
}

export function parseSseEventBlock(block: string): SseEventMessage | null {
  let event = "message";
  let id: string | undefined;
  const data: string[] = [];
  for (const rawLine of block.split(/\r?\n/)) {
    if (!rawLine || rawLine.startsWith(":")) continue;
    const separator = rawLine.indexOf(":");
    const field = separator === -1 ? rawLine : rawLine.slice(0, separator);
    let value = separator === -1 ? "" : rawLine.slice(separator + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") event = value || "message";
    else if (field === "data") data.push(value);
    else if (field === "id") id = value;
  }
  if (data.length === 0) return null;
  return { event, data: data.join("\n"), id };
}

export async function streamSse(
  path: string,
  init: RequestInit & {
    timeoutMs?: number;
    onEvent: (message: SseEventMessage) => void;
  },
): Promise<void> {
  const controller = new AbortController();
  let didTimeout = false;
  const idleTimeout = init.timeoutMs ?? 120_000;
  let timeoutId: number | null = null;
  const resetIdleTimeout = () => {
    if (timeoutId !== null) window.clearTimeout(timeoutId);
    timeoutId = window.setTimeout(() => {
      didTimeout = true;
      controller.abort();
    }, idleTimeout);
  };
  resetIdleTimeout();
  const forwardAbort = () => controller.abort();
  init.signal?.addEventListener("abort", forwardAbort, { once: true });

  try {
    if (init.signal?.aborted) {
      throw new DOMException("The request was cancelled.", "AbortError");
    }
    const method = (init.method ?? "GET").toUpperCase();
    const csrfToken = typeof document !== "undefined"
      ? document.cookie
          .split("; ")
          .find((entry) => entry.startsWith("agentfixb_csrf="))
          ?.split("=")
          .slice(1)
          .join("=")
      : undefined;
    const headers = new Headers(init.headers);
    headers.set("Accept", "text/event-stream");
    if (!(init.body instanceof FormData)) headers.set("Content-Type", "application/json");
    if (csrfToken && !["GET", "HEAD", "OPTIONS"].includes(method)) {
      headers.set("X-CSRF-Token", decodeURIComponent(csrfToken));
    }

    const response = await fetch(`${BASE}${path}`, {
      ...init,
      credentials: "include",
      signal: controller.signal,
      headers,
    });
    if (!response.ok) {
      const payload = (await readResponseBody(response)) as ApiErrorPayload | string | null;
      const detail = typeof payload === "object" && payload !== null ? payload.detail : payload;
      const message = typeof payload === "object" && payload !== null
        ? payload.message || (typeof payload.detail === "string" ? payload.detail : null)
        : typeof payload === "string" ? payload : null;
      throw new ApiError(message || "Could not start the AI request.", response.status, detail);
    }
    if (!response.body) throw new ApiError("The AI response stream was empty.", response.status);

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      if (value && value.byteLength > 0) resetIdleTimeout();
      buffer += decoder.decode(value, { stream: !done });
      const blocks = buffer.split(/\r?\n\r?\n/);
      buffer = blocks.pop() ?? "";
      for (const block of blocks) {
        const message = parseSseEventBlock(block);
        if (message) init.onEvent(message);
      }
      if (done) break;
    }
    const finalMessage = parseSseEventBlock(buffer);
    if (finalMessage) init.onEvent(finalMessage);
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (didTimeout) {
      throw new ApiError(`The AI response stream was idle for ${Math.round(idleTimeout / 1000)} seconds.`);
    }
    if (init.signal?.aborted) throw new DOMException("The request was cancelled.", "AbortError");
    throw new ApiError(error instanceof Error && error.message ? error.message : "The AI stream failed.");
  } finally {
    if (timeoutId !== null) window.clearTimeout(timeoutId);
    init.signal?.removeEventListener("abort", forwardAbort);
  }
}

export type DashboardRole = "Viewer" | "Editor" | "Admin";
export type DashboardUserStatus = "active" | "disabled" | "pending_verification";

export interface AuthConfig {
  auth_enabled: boolean;
  mode: "local" | "entra";
  local_registration_enabled: boolean;
  allowed_email_domains: string[];
  password_min_length: number;
}

export interface LocalLoginPayload {
  email: string;
  password: string;
}

export interface RegisterPayload extends LocalLoginPayload {
  display_name: string;
}

export interface AuthActionResult {
  accepted: boolean;
  message: string;
}

export interface EmailVerificationResult {
  verified: boolean;
  email: string | null;
  message: string;
}

export interface PasswordResetResult {
  reset: boolean;
  message: string;
}

export interface AdminPasswordResetResult extends AuthActionResult {
  delivered: boolean;
}

export interface AuthSession {
  authenticated: boolean;
  auth_enabled: boolean;
  user_id?: string;
  operator_id?: string;
  email?: string | null;
  display_name?: string;
  username?: string;
  role?: DashboardRole;
  status?: DashboardUserStatus;
  auth_method?: "local" | "entra" | "emergency" | "legacy" | "disabled" | string;
  session_id?: string | null;
  permissions?: string[];
}

export interface EmergencyLoginPayload {
  username: string;
  password: string;
}

export interface AuthSessionRecord {
  session_id: string;
  auth_method: string;
  ip_address: string | null;
  user_agent: string | null;
  created_at: string;
  last_seen_at: string;
  idle_expires_at: string;
  absolute_expires_at: string;
  revoked_at: string | null;
  revoke_reason: string | null;
  current: boolean;
  active: boolean;
}

export interface AdminUser {
  user_id: string;
  email: string | null;
  display_name: string;
  role: DashboardRole;
  status: DashboardUserStatus;
  account_type: string;
  is_primary_admin: number | boolean;
  email_verified_at: string | null;
  last_login_at: string | null;
  created_at: string;
  updated_at: string;
  active_session_count: number;
  telegram_user_id?: string | null;
}

export interface SecurityEvent {
  event_id: string;
  event_type: string;
  severity: string;
  actor_user_id: string | null;
  subject_user_id: string | null;
  session_id: string | null;
  ip_address: string | null;
  user_agent: string | null;
  metadata: Record<string, unknown>;
  created_at: string;
}

function safeRedirectPath(value: string): string {
  return value.startsWith("/") && !value.startsWith("//") ? value : "/";
}

export function getEntraLoginUrl(redirectTo = "/"): string {
  const query = new URLSearchParams({ redirect_to: safeRedirectPath(redirectTo) });
  return `${BASE}/api/auth/entra/login?${query.toString()}`;
}

export const getAuthConfig = () => fetchJson<AuthConfig>("/api/auth/config");
export const getAuthSession = () => fetchJson<AuthSession>("/api/auth/session");
export const login = (email: string, password: string) =>
  fetchJson<AuthSession>("/api/auth/login", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  });
export const register = (payload: RegisterPayload) =>
  fetchJson<AuthActionResult>("/api/auth/register", {
    method: "POST",
    body: JSON.stringify(payload),
  });
export const verifyEmail = (token: string) =>
  fetchJson<EmailVerificationResult>("/api/auth/verify-email", {
    method: "POST",
    body: JSON.stringify({ token }),
  });
export const resendVerification = (email: string) =>
  fetchJson<AuthActionResult>("/api/auth/resend-verification", {
    method: "POST",
    body: JSON.stringify({ email }),
  });
export const forgotPassword = (email: string) =>
  fetchJson<AuthActionResult>("/api/auth/forgot-password", {
    method: "POST",
    body: JSON.stringify({ email }),
  });
export const resetPassword = (token: string, password: string) =>
  fetchJson<PasswordResetResult>("/api/auth/reset-password", {
    method: "POST",
    body: JSON.stringify({ token, password }),
  });
export const emergencyLogin = (payload: EmergencyLoginPayload) =>
  fetchJson<AuthSession>("/api/auth/emergency/login", {
    method: "POST",
    body: JSON.stringify(payload),
  });
export const logout = () =>
  fetchJson<{ authenticated: boolean }>("/api/auth/logout", { method: "POST" });
export const logoutAll = () =>
  fetchJson<{ authenticated: boolean; revoked_sessions: number }>("/api/auth/logout-all", { method: "POST" });
export const getMySessions = async () =>
  (await fetchJson<{ sessions: AuthSessionRecord[] }>("/api/auth/sessions")).sessions;
export const revokeMySession = (sessionId: string) =>
  fetchJson<{ revoked: boolean; session_id: string }>(`/api/auth/sessions/${encodeURIComponent(sessionId)}`, {
    method: "DELETE",
  });
export const getAdminUsers = async (search?: string) => {
  const query = search?.trim() ? `?${new URLSearchParams({ search: search.trim() })}` : "";
  return (await fetchJson<{ users: AdminUser[] }>(`/api/admin/users${query}`)).users;
};
export const updateAdminUser = async (
  userId: string,
  update: { role?: DashboardRole; status?: DashboardUserStatus },
) => (
  await fetchJson<{ user: AdminUser }>(`/api/admin/users/${encodeURIComponent(userId)}`, {
    method: "PATCH",
    body: JSON.stringify(update),
  })
).user;
export const getAdminUserSessions = async (userId: string, includeTerminated = false) => {
  const query = includeTerminated ? "?include_terminated=true" : "";
  return (
    await fetchJson<{ sessions: AuthSessionRecord[] }>(
      `/api/admin/sessions/users/${encodeURIComponent(userId)}${query}`,
    )
  ).sessions;
};
export const revokeAdminSession = (sessionId: string) =>
  fetchJson<{ revoked: boolean; session_id: string }>(
    `/api/admin/sessions/${encodeURIComponent(sessionId)}`,
    { method: "DELETE" },
  );
export const revokeAllAdminUserSessions = (userId: string) =>
  fetchJson<{ revoked_sessions: number; user_id: string }>(
    `/api/admin/sessions/users/${encodeURIComponent(userId)}/revoke-all`,
    { method: "POST" },
  );
export const sendAdminPasswordReset = (userId: string) =>
  fetchJson<AdminPasswordResetResult>(
    `/api/admin/users/${encodeURIComponent(userId)}/password-reset`,
    { method: "POST" },
  );
export const bindAdminTelegramIdentity = async (userId: string, telegramUserId: string) =>
  (
    await fetchJson<{ user: AdminUser }>(
      `/api/admin/users/${encodeURIComponent(userId)}/telegram-identity`,
      {
        method: "PUT",
        body: JSON.stringify({ telegram_user_id: telegramUserId }),
      },
    )
  ).user;
export const unbindAdminTelegramIdentity = async (userId: string) =>
  (
    await fetchJson<{ user: AdminUser }>(
      `/api/admin/users/${encodeURIComponent(userId)}/telegram-identity`,
      { method: "DELETE" },
    )
  ).user;
export const getSecurityEvents = async (filters?: { eventType?: string; limit?: number }) => {
  const query = new URLSearchParams();
  if (filters?.eventType?.trim()) query.set("event_type", filters.eventType.trim());
  if (filters?.limit) query.set("limit", String(filters.limit));
  const queryString = query.toString();
  const suffix = queryString ? `?${queryString}` : "";
  return (await fetchJson<{ events: SecurityEvent[] }>(`/api/admin/security-events${suffix}`)).events;
};

export interface PersonalBriefItem {
  id: string;
  type: string;
  title: string;
  reason: string;
  due_at?: string;
  source_ref?: string;
  suggested_action?: string;
}

export interface PersonalBrief {
  snapshot_id: string;
  generated_at: string;
  summary: Record<string, number>;
  sections: Record<string, PersonalBriefItem[]>;
}

export const getPersonalBrief = (refresh = false) =>
  fetchJson<PersonalBrief>(`/api/assistant/brief${refresh ? "?refresh=true" : ""}`);

export interface ApprovalIntent {
  action_id: string;
  action_type: string;
  risk_class: "low" | "medium" | "high" | "critical";
  target_type: string;
  target_id: string;
  target_version?: string;
  payload: Record<string, unknown>;
  status: string;
  created_by_surface: string;
  created_at: string;
  expires_at: string;
  error?: string;
  result_ref?: string;
}

export const getApprovals = (status?: string) =>
  fetchJson<ApprovalIntent[]>(`/api/approvals${status ? `?status=${encodeURIComponent(status)}` : ""}`);
export const approveIntent = (actionId: string, execute = false) =>
  fetchJson<ApprovalIntent>(`/api/approvals/${actionId}/approve${execute ? "?execute=true" : ""}`, {
    method: "POST",
    timeoutMs: execute ? SEND_TIMEOUT_MS : REQUEST_TIMEOUT_MS,
  });
export const rejectIntent = (actionId: string) =>
  fetchJson<ApprovalIntent>(`/api/approvals/${actionId}/reject`, { method: "POST" });
export const requestProjectCompletion = (projectId: string) =>
  fetchJson<ApprovalIntent>(
    `/api/projects/${encodeURIComponent(projectId)}/completion-approval`,
    { method: "POST" },
  );

export interface InboxInsight {
  insight_id: string;
  email_id: string;
  priority: "low" | "normal" | "high" | "urgent";
  priority_reasons: string[];
  action_required: boolean;
  request_summary?: string;
  suggested_action?: string;
  due_at?: string;
  proposed_owner?: string;
  related_entities?: Array<{ type?: string; id?: string }>;
  evidence_spans: string[];
  confidence: number;
  review_status: string;
  category: string;
  suppressed: boolean;
}

export const getInboxInsights = () =>
  fetchJson<InboxInsight[]>("/api/inbox/insights");

export const backfillInboxInsights = (limit = 1) =>
  fetchJson<{
    requested: number;
    analyzed: number;
    suppressed: number;
    has_more?: boolean;
    remaining_unanalyzed?: number;
    errors: string[];
    email_ids: string[];
  }>("/api/inbox/insights/backfill?limit=" + encodeURIComponent(String(limit)), {
    method: "POST",
    body: "{}",
    timeoutMs: 90_000,
  });

export interface CalendarEvent {
  calendar_event_id: string;
  subject: string;
  start_at: string;
  end_at: string;
  original_timezone?: string;
  location?: string;
  organizer_email?: string;
  attendees: Array<Record<string, unknown>>;
  is_online_meeting: boolean;
  join_url?: string;
  body_preview?: string;
  cancelled: boolean;
}

export interface MeetingPrep {
  prep_id: string;
  calendar_event_id: string;
  project_id?: string;
  meeting: CalendarEvent;
  attendees: Array<Record<string, unknown>>;
  project_context?: Record<string, unknown>;
  open_work_items: Array<Record<string, unknown>>;
  agenda: string[];
  open_decisions: string[];
  talking_points: string[];
  provenance: Array<{ source_type: string; source_ref: string }>;
}

export interface MeetingIngestion {
  ingestion_id: string;
  calendar_event_id: string;
  work_item_ids: string[];
  created_at: string;
}

export const getCalendarEvents = (days = 7) =>
  fetchJson<CalendarEvent[]>(`/api/calendar/events?days=${days}`);
export const refreshCalendar = () =>
  fetchJson<Record<string, unknown>>("/api/calendar/refresh", { method: "POST" });
export const getMeetingPrep = (eventId: string) =>
  fetchJson<MeetingPrep>(`/api/calendar/events/${encodeURIComponent(eventId)}/prep`);
export const prepareMeeting = (eventId: string, projectId?: string) =>
  fetchJson<MeetingPrep>(`/api/calendar/events/${encodeURIComponent(eventId)}/prep`, {
    method: "POST",
    body: JSON.stringify({ project_id: projectId }),
  });
export const ingestMeetingNotes = (eventId: string, sourceText: string) =>
  fetchJson<MeetingIngestion>(`/api/calendar/events/${encodeURIComponent(eventId)}/ingestions`, {
    method: "POST",
    body: JSON.stringify({ source_text: sourceText }),
    timeoutMs: LLM_TIMEOUT_MS,
  });
export const proposeCalendarAction = (payload: {
  operation: "create" | "update" | "cancel";
  calendar_event_id?: string;
  event?: Record<string, unknown>;
}) => fetchJson<ApprovalIntent>("/api/calendar/actions", {
  method: "POST",
  body: JSON.stringify(payload),
});

// Dashboard
export const getDashboard = () => fetchJson<DashboardData>("/api/dashboard");

// Projects
export const getProjects = (params?: Record<string, string>) => {
  const qs = params ? "?" + new URLSearchParams(params).toString() : "";
  return fetchJson<ProjectSummary[]>(`/api/projects${qs}`);
};

export const getProject = (id: string) =>
  fetchJson<ProjectDetail>(`/api/projects/${id}`);
export const transferProjectOwner = (id: string, ownerUserId: string) =>
  fetchJson<ProjectDetail>(`/api/projects/${encodeURIComponent(id)}/owner`, {
    method: "PATCH",
    body: JSON.stringify({ owner_user_id: ownerUserId }),
  });

// Activity
export const getActivity = (projectId: string) =>
  fetchJson<ActivityEntry[]>(`/api/projects/${projectId}/activity`);

export const getGlobalActivity = async (limit = 8): Promise<ActivityEntry[]> => {
  const res = await fetchJson<{ items: ActivityEntry[]; has_more: boolean }>(
    `/api/activity?limit=${limit}`,
  );
  return res.items;
};

export const getActionTypes = () =>
  fetchJson<string[]>("/api/activity/action-types");

export interface ActivityFilters {
  actor?: string;
  action_type?: string;
  project_id?: string;
  since?: string;
  until?: string;
}

export interface PaginatedActivity {
  items: ActivityEntry[];
  has_more: boolean;
}

export const getFilteredActivity = (
  filters: ActivityFilters = {},
  limit = 50,
  offset = 0,
): Promise<PaginatedActivity> => {
  const params = new URLSearchParams({
    limit: String(limit),
    offset: String(offset),
  });
  for (const [k, v] of Object.entries(filters)) {
    if (v) params.set(k, v);
  }
  return fetchJson<PaginatedActivity>(`/api/activity?${params.toString()}`);
};

// Evidence
export const getEvidence = (projectId: string) =>
  fetchJson<EvidenceItem[]>(`/api/projects/${projectId}/evidence`);

// Milestones
export const getMilestones = (projectId: string) =>
  fetchJson<Record<string, unknown>[]>(`/api/projects/${projectId}/milestones`);

// Memory
export const getMemory = (projectId: string) =>
  fetchJson<MemoryItem[]>(`/api/projects/${projectId}/memory`);

// Drafts
export const getDrafts = () => fetchJson<Draft[]>("/api/drafts");

export const getDraft = (id: string) => fetchJson<Draft>(`/api/drafts/${id}`);

export interface StandaloneDraftPayload {
  context: string;
  to_email?: string;
  contact_id?: string;
  contact_name?: string;
  subject?: string;
  body?: string;
  cc?: string;
  purpose?: string;
  related_project_id?: string;
  scheduled_send_at?: string;
}

export const createStandaloneDraft = (payload: StandaloneDraftPayload) =>
  fetchJson<{ draft_id: string; to_email: string; subject: string; contact_id?: string }>(
    "/api/drafts",
    { method: "POST", body: JSON.stringify(payload) },
  );

export const draftAction = (id: string, action: "approve" | "discard") =>
  fetchJson<ActionResult>(`/api/drafts/${id}/action`, {
    method: "POST",
    body: JSON.stringify({ action }),
    timeoutMs: action === "approve" ? SEND_TIMEOUT_MS : REQUEST_TIMEOUT_MS,
  });

export const approveAndSendDraft = async (id: string): Promise<ActionResult> => {
  const result = await draftAction(id, "approve");
  if (!result.success || !result.action_id) {
    return result;
  }

  const intent = await approveIntent(result.action_id, true);
  if (intent.status === "executed") {
    return { success: true, message: "Draft approved and sent" };
  }
  return {
    success: false,
    message: intent.error || `Draft send did not complete (status: ${intent.status})`,
  };
};

// Bulk draft action
export const bulkDraftAction = (draftIds: string[], action: "approve" | "discard") =>
  fetchJson<{ results: Array<{ draft_id: string; success: boolean; message: string }> }>(
    "/api/drafts/bulk-action",
    { method: "POST", body: JSON.stringify({ draft_ids: draftIds, action }) }
  );

// Reviews
export const getReviews = () => fetchJson<ReviewItem[]>("/api/reviews");

export const reviewAction = (
  updateId: string,
  action: "approve" | "reject",
  rejectPayload?: { reject_tags: string[]; reject_reason?: string },
) =>
  fetchJson<ActionResult>(`/api/reviews/${updateId}/action`, {
    method: "POST",
    body: JSON.stringify({ action, ...rejectPayload }),
  });

// Aggregate query
export const runAggregateQuery = (text: string, limit = 20) =>
  fetchJson<AggregateQueryResponse>(
    `/api/query?${new URLSearchParams({ text, limit: String(limit) }).toString()}`
  );

export const getQueryCsvUrl = (text: string) =>
  `${BASE}/api/query?${new URLSearchParams({ text, format: "csv" }).toString()}`;

// Traces
export interface LLMSummary {
  total_calls: number;
  total_tokens: number;
  avg_latency_ms: number;
  error_count: number;
  error_rate_pct: number;
  period_days: number;
}

export interface LLMTaskBreakdown {
  task: string;
  count: number;
  avg_tokens: number;
  avg_latency_ms: number;
  error_count: number;
}

export interface LLMDailyTrend {
  date: string;
  calls: number;
  tokens: number;
  errors: number;
  avg_latency_ms: number;
}

export const getLLMAnalyticsSummary = (days: number) =>
  fetchJson<LLMSummary>(`/api/llm-analytics/summary?days=${days}`);
export const getLLMAnalyticsByTask = (days: number) =>
  fetchJson<{ tasks: LLMTaskBreakdown[] }>(`/api/llm-analytics/by-task?days=${days}`);
export const getLLMAnalyticsTrends = (days: number) =>
  fetchJson<{ days: LLMDailyTrend[] }>(`/api/llm-analytics/trends?days=${days}`);

export const getTraces = (params?: Record<string, string>) => {
  const qs = params ? "?" + new URLSearchParams(params).toString() : "";
  return fetchJson<TraceSummary[]>(`/api/traces${qs}`);
};

export const getTraceDetail = (traceId: string) =>
  fetchJson<TraceDetail>(`/api/traces/${traceId}`);


export const getTraceRuns = (params?: Record<string, string>) => {
  const qs = params ? "?" + new URLSearchParams(params).toString() : "";
  return fetchJson<TraceRun[]>(`/api/traces/runs${qs}`);
};
export const getTraceRunDetail = (runId: string) =>
  fetchJson<TraceRunDetail>(`/api/traces/runs/${encodeURIComponent(runId)}`);
export const diagnoseTraceRun = (runId: string) =>
  fetchJson<TraceDiagnostic>(`/api/traces/runs/${encodeURIComponent(runId)}/diagnose`, {
    method: "POST",
  });
export const revealTraceEvent = (eventId: string) =>
  fetchJson<{ event_id: string; payload: unknown }>(
    `/api/traces/events/${encodeURIComponent(eventId)}/reveal`,
    { method: "POST" },
  );

// Telegram
export const sendToTelegram = (
  text: string,
  projectId?: string,
  chatId?: number
) =>
  fetchJson<{ ok: boolean; message_id?: number }>("/api/telegram/send", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text, project_id: projectId, chat_id: chatId }),
  });

// Closing checklist
export type AdvanceProjectToM8Payload = {
  evidence_ref: string;
  expected_project_updated_at: string;
  reason: string;
};

export type ClosingItemConfirmPayload = {
  evidence_ref?: string | null;
  note?: string | null;
  manual_attestation: boolean;
};

export type CompleteProjectClosePayload = {
  expected_project_updated_at: string;
  confirm_terminal: true;
  sign_off_note: string;
  acknowledgements: string[];
};

export const getProjectClosing = (projectId: string) =>
  fetchJson<ClosingStatusResponse>(`/api/projects/${projectId}/closing`);

export const advanceProjectToM8 = (
  projectId: string,
  payload: AdvanceProjectToM8Payload,
) => fetchJson<{ success: boolean; message: string; project_id: string }>(
  `/api/projects/${projectId}/closing/advance-to-m8`,
  { method: "POST", body: JSON.stringify(payload) },
);

export const initiateClosing = (projectId: string) =>
  fetchJson<{ message: string }>(`/api/projects/${projectId}/closing/initiate`, {
    method: "POST",
    body: "{}",
  });

export const closingItemConfirm = (
  projectId: string,
  itemId: string,
  payload: ClosingItemConfirmPayload,
) => fetchJson<{ success: boolean; status: string }>(
  `/api/projects/${projectId}/closing/${itemId}/confirm`,
  { method: "POST", body: JSON.stringify(payload) },
);

export const closingItemDismiss = (projectId: string, itemId: string, reason: string) =>
  fetchJson<{ success: boolean; status: string }>(
    `/api/projects/${projectId}/closing/${itemId}/dismiss`,
    { method: "POST", body: JSON.stringify({ reason }) },
  );

export const completeProjectClose = (
  projectId: string,
  payload: CompleteProjectClosePayload,
) => fetchJson<{ success: boolean; message: string }>(
  `/api/projects/${projectId}/close`,
  { method: "POST", body: JSON.stringify(payload) },
);

// Preferences
export const getPreferences = () =>
  fetchJson<OperatorPreference[]>("/api/preferences");

export const createPreference = (pref: Omit<OperatorPreference, "id" | "updated_at">) =>
  fetchJson<{ id: number; message: string }>("/api/preferences", {
    method: "POST",
    body: JSON.stringify(pref),
  });

export const deletePreference = (id: number) =>
  fetchJson<{ success: boolean; message: string }>(`/api/preferences/${id}`, {
    method: "DELETE",
  });

// Draft edit (body, to_email, cc)
export const updateDraft = (
  draftId: string,
  fields: { body?: string; to_email?: string; cc?: string },
) =>
  fetchJson<Draft>(`/api/drafts/${draftId}`, {
    method: "PATCH",
    body: JSON.stringify(fields),
  });

/** @deprecated Use updateDraft instead */
export const updateDraftBody = (draftId: string, body: string) =>
  updateDraft(draftId, { body });

// Reports
export const getReport = (type: "weekly" | "monthly", date?: string) => {
  const qs = new URLSearchParams({ type, ...(date ? { date } : {}) }).toString();
  return fetchJson<ReportData>(`/api/reports?${qs}`);
};

// Intake
export const createProjectIntake = (payload: IntakePayload) =>
  fetchJson<IntakeResult>("/api/projects/intake", {
    method: "POST",
    body: JSON.stringify(payload),
  });

// Re-export types needed by pages
export type { ClosingItem, OperatorPreference, ReportData, IntakePayload, IntakeResult, PortfolioHealthData } from "./types";

// Portfolio health
export const fetchPortfolioHealth = () =>
  fetchJson<PortfolioHealthData>("/api/portfolio/health");

export const postAiFeedback = (body: {
  item_type: string;
  item_id: string;
  project_id?: string;
  feedback_kind: string;
  category?: string;
}) =>
  fetchJson<{ success: boolean; feedback_id: string }>("/api/feedback", {
    method: "POST",
    body: JSON.stringify(body),
  });

// Notification preferences
export const fetchNotificationPrefs = () =>
  fetchJson<Record<string, unknown>>("/api/preferences/notifications");

export const updateNotificationPrefs = (prefs: Record<string, unknown>) =>
  fetchJson<{ success: boolean }>("/api/preferences/notifications", {
    method: "PATCH",
    body: JSON.stringify(prefs),
  });

// LLM Config
export interface LLMConfig {
  active_provider: string;
  active_model: string;
  openrouter_api_key: string;
  openai_api_key: string;
  anthropic_api_key: string;
  gemini_api_key: string;
  minimax_api_key: string;
  ollama_api_base: string;
  ollama_api_key: string;
  local_llm_api_base: string;
  local_llm_api_key: string;
  local_llm_model: string;
  vertex_ai_project: string;
  vertex_ai_location: string;
  vertex_ai_model: string;
  vertex_ai_credentials_path: string;
}

export const getLLMConfig = () => fetchJson<LLMConfig>("/api/llm-config");

export const updateLLMConfig = (config: Partial<LLMConfig>) =>
  fetchJson<{ success: boolean; provider: string; model: string }>("/api/llm-config", {
    method: "PATCH",
    body: JSON.stringify(config),
  });

// LLM Task Routing
export interface TaskRoute {
  task: string;
  label: string;
  model: string;
  api_base: string;
  temperature: number;
  max_tokens: number | null;
  has_override: boolean;
}

export const getLLMRouting = () => fetchJson<TaskRoute[]>("/api/llm-config/routing");

export const updateLLMRouting = (overrides: { task: string; model?: string; temperature?: number }[]) =>
  fetchJson<{ success: boolean; overrides_count: number }>("/api/llm-config/routing", {
    method: "PATCH",
    body: JSON.stringify(overrides),
  });

// Save operator notes
export const saveProjectNotes = (projectId: string, notes: string) =>
  fetchJson<{ ok: boolean }>(`/api/projects/${projectId}/notes`, {
    method: "PATCH",
    body: JSON.stringify({ notes }),
  });

export const createProjectDraft = (
  projectId: string,
  body: { draft_body: string; subject?: string; task_type?: string },
) =>
  fetchJson<{ draft_id: string }>(`/api/projects/${projectId}/drafts`, {
    method: "POST",
    body: JSON.stringify(body),
  });

export const flagProject = (projectId: string, flag = "urgent") =>
  fetchJson<{ ok?: boolean }>(`/api/projects/${projectId}/flag`, {
    method: "POST",
    body: JSON.stringify({ flag }),
  });

export const getProjectFollowups = (projectId: string) =>
  fetchJson<Array<{ followup_id: string; status: string } & Record<string, unknown>>>(
    `/api/projects/${projectId}/followups`,
  );

// AI draft email suggestion
export const getProjectDraftSuggestion = (projectId: string) =>
  fetchJson<{ draft_body: string; ball_with: string; subject: string }>(
    `/api/projects/${projectId}/draft-suggestion`,
    { timeoutMs: LLM_TIMEOUT_MS },
  );

export const getProjectContacts = (projectId: string) =>
  fetchJson<import("./types").ProjectContact[]>(`/api/projects/${projectId}/contacts`);

export interface ProjectParty {
  id?: number;
  project_id: string;
  org_id: string;
  party_role: string;
  is_primary?: number | boolean;
  org_name?: string;
  org_type?: string;
  domains?: string | null;
}

export const getProjectParties = (projectId: string) =>
  fetchJson<{ project_id: string; parties: ProjectParty[] }>(
    `/api/projects/${projectId}/parties`,
  );

export interface OrganisationSummary {
  org_id: string;
  org_name: string;
  org_type: string | null;
  domains: string | null;
}

export const listOrganisations = (orgType?: string, limit = 100) => {
  const params = new URLSearchParams();
  if (orgType) params.set("org_type", orgType);
  if (limit !== 100) params.set("limit", String(limit));
  const qs = params.toString();
  return fetchJson<{ items: OrganisationSummary[]; count: number }>(
    `/api/parties${qs ? `?${qs}` : ""}`,
  );
};

export const getOrganisation = (orgId: string) =>
  fetchJson<{
    organisation: OrganisationSummary & Record<string, unknown>;
    projects: Array<Record<string, unknown>>;
    contacts: Array<Record<string, unknown>>;
  }>(`/api/parties/${encodeURIComponent(orgId)}`);

// Project chat history
export const getProjectChatHistory = (projectId: string) =>
  fetchJson<ChatHistoryResponse>(`/api/projects/${projectId}/chat`);

// Send project chat message
export const sendProjectChatMessage = (
  projectId: string,
  message: string,
  sessionMessages: ChatMessage[] = [],
) =>
  fetchJson<ChatSendResponse>(`/api/projects/${projectId}/chat`, {
    method: "POST",
    body: JSON.stringify({ message, session_messages: sessionMessages }),
    timeoutMs: LLM_TIMEOUT_MS,
  });

// Conversation context
export const getConversationContext = (chatId?: number) => {
  const qs = chatId ? `?chat_id=${chatId}` : "";
  return fetchJson<import("./types").ConversationContext>(`/api/conversation-context${qs}`);
};

export const clearConversationSummary = (chatId?: number) => {
  const qs = chatId ? `?chat_id=${chatId}` : "";
  return fetchJson<{ ok: boolean }>(`/api/conversation-context${qs}`, {
    method: "DELETE",
  });
};

export const previewAgentContext = (message: string, chatId?: number) => {
  return fetchJson<import("./types").ContextPreviewResponse>("/api/context-preview", {
    method: "POST",
    body: JSON.stringify({ message, chat_id: chatId || undefined }),
    timeoutMs: LLM_TIMEOUT_MS,
  });
};

// Dashboard home sections
export async function getOverdueFollowups(): Promise<OverdueFollowup[]> {
  try {
    return await fetchJson<OverdueFollowup[]>("/api/dashboard/overdue-followups");
  } catch {
    return [];
  }
}

export async function getComingUp(days = 3): Promise<ComingUpData> {
  try {
    return await fetchJson<ComingUpData>(`/api/dashboard/coming-up?days=${days}`);
  } catch {
    return { today: [], tomorrow: [], day2: [] };
  }
}

export async function markFollowupDone(followupId: string, expectedRevision?: number): Promise<boolean> {
  try {
    await fetchJson<unknown>(`/api/followups/${followupId}/action`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'done', ...(expectedRevision !== undefined ? { expected_revision: expectedRevision } : {}) }),
    });
    return true;
  } catch {
    return false;
  }
}

// ─── Learning ────────────────────────────────────────────────

export type KnowledgeEntry = {
  entry_id: string;
  created_at: string;
  thread_id: string | null;
  project_id: string | null;
  project_name: string | null;
  ticket_id: string | null;
  knowledge_type: string;
  title: string;
  content: string;
  confidence: string;
  visibility: string;
  wiki_path: string | null;
  source_email_count: number;
};

export type SourceEmail = {
  email_id: string;
  sender_email: string;
  subject: string;
  direction: string;
  processed_at: string;
};

export type KnowledgeEntryDetail = KnowledgeEntry & {
  source_emails: SourceEmail[];
};

export type LearningStats = {
  total_entries: number;
  by_type: Record<string, number>;
  by_visibility: Record<string, number>;
  by_confidence: Record<string, number>;
  last_run: {
    run_id: string;
    started_at: string;
    threads_processed: number;
    entries_created: number;
    wiki_pages_written: number;
    errors: string[];
  } | null;
};

export async function getLearningRecent(params?: {
  days?: number;
  knowledge_type?: string;
  visibility?: string;
}): Promise<KnowledgeEntry[]> {
  const q = new URLSearchParams();
  if (params?.days) q.set("days", String(params.days));
  if (params?.knowledge_type) q.set("knowledge_type", params.knowledge_type);
  if (params?.visibility) q.set("visibility", params.visibility);
  const qs = q.toString();
  return fetchJson(`/api/learning/recent${qs ? `?${qs}` : ""}`);
}

export async function getLearningStats(): Promise<LearningStats> {
  return fetchJson("/api/learning/stats");
}

export async function getLearningEntry(entryId: string): Promise<KnowledgeEntryDetail> {
  return fetchJson(`/api/learning/entry/${entryId}`);
}

// ─── Timeline ──────────────────────────────────────────────

export interface TimelineProject {
  project_id: string;
  client_name: string | null;
  project_status: string;
  current_milestone: string | null;
  ball_with: string | null;
  target_go_live: string | null;
  project_tier: string | null;
  health: "green" | "yellow" | "red";
  schedule_status: "overdue" | "due_soon" | "scheduled" | "no_date" | "invalid";
  days_to_go_live: number | null;
}

export const getTimeline = () => fetchJson<TimelineProject[]>("/api/timeline");

// ─── LLM Status ─────────────────────────────────────────────

export interface LLMProviderStatus {
  name: string;
  reachable: boolean;
  latency_ms: number | null;
  model: string;
}

export interface LLMHealthResponse {
  primary: LLMProviderStatus;
  backup: LLMProviderStatus;
  active_provider: "primary" | "backup" | "none";
}

export async function getLLMStatus(): Promise<LLMHealthResponse> {
  return fetchJson<LLMHealthResponse>("/api/llm-status");
}

// ── Attachments ──────────────────────────────────────────────────────────

export async function listAttachments(draftId: string): Promise<Attachment[]> {
  return fetchJson<Attachment[]>(`/api/drafts/${draftId}/attachments`);
}

export async function uploadAttachment(draftId: string, file: File): Promise<Attachment> {
  const formData = new FormData();
  formData.append("file", file);
  return fetchJson<Attachment>(`/api/drafts/${draftId}/attachments`, {
    method: "POST",
    body: formData,
  });
}

export async function deleteAttachment(draftId: string, filename: string): Promise<void> {
  await fetchJson<{ success: boolean }>(
    `/api/drafts/${draftId}/attachments/${encodeURIComponent(filename)}`,
    { method: "DELETE" },
  );
}

// Canonical FIX session configuration registry
export const getProjectFixSessions = (projectId: string) =>
  fetchJson<import("./types").FixSessionRecord[]>(`/api/projects/${projectId}/fix-sessions`);

export const getFixSessionHistory = (
  projectId: string,
  environment: string,
  sessionKey: string,
) =>
  fetchJson<import("./types").FixSessionHistoryRecord[]>(
    `/api/projects/${projectId}/fix-sessions/${encodeURIComponent(environment)}/${encodeURIComponent(sessionKey)}/history`,
  );

export const getPendingFixSessionCandidates = (projectId?: string) => {
  const qs = projectId ? `?project_id=${encodeURIComponent(projectId)}` : "";
  return fetchJson<import("./types").FixSessionCandidate[]>(`/api/fix-session-candidates${qs}`);
};

export const approveFixSessionCandidate = (
  candidateId: string,
  reason: string,
) =>
  fetchJson<import("./types").FixSessionDecisionResult>(
    `/api/fix-session-candidates/${encodeURIComponent(candidateId)}/approve`,
    { method: "POST", body: JSON.stringify({ reason }) },
  );

export const rejectFixSessionCandidate = (
  candidateId: string,
  reason: string,
) =>
  fetchJson<import("./types").FixSessionDecisionResult>(
    `/api/fix-session-candidates/${encodeURIComponent(candidateId)}/reject`,
    { method: "POST", body: JSON.stringify({ reason }) },
  );


// Committed project update notification delivery (not project mutations).
export type UpdateNotification = import("./generated/api-schema").components["schemas"]["NotificationStatusResponse"];
export type NotificationReview = import("./generated/api-schema").components["schemas"]["NotificationReviewRequest"];
export const getUpdateNotifications = (projectId: string) =>
  fetchJson<UpdateNotification[]>(`/api/projects/${encodeURIComponent(projectId)}/update-notifications?limit=50`);
export const reconcileUpdateNotification = (projectId: string, updateId: string, body: NotificationReview) =>
  fetchJson<UpdateNotification>(`/api/projects/${encodeURIComponent(projectId)}/updates/${encodeURIComponent(updateId)}/notification/reconcile`, {
    method: "POST", body: JSON.stringify(body),
  });

// Atomic project action plans share the existing follow-up ledger.
export const getActionPlan = (projectId: string) =>
  fetchJson<import("./action-plan").ActionPlan>(`/api/projects/${encodeURIComponent(projectId)}/action-plan`);
export const getActionPlanReviews = (projectId: string) =>
  fetchJson<import("./action-review").ActionReview[]>(`/api/projects/${encodeURIComponent(projectId)}/action-plan/reviews`);
export const decideActionPlanReview = (projectId: string, reviewId: string, body: import("./action-review").ReviewDecision) =>
  fetchJson<import("./action-review").ReviewDecisionResult>(`/api/projects/${encodeURIComponent(projectId)}/action-plan/reviews/${encodeURIComponent(reviewId)}/decisions`, {
    method: "POST", body: JSON.stringify(body),
  });
export const applyActionPlanBundle = async (projectId: string, body: import("./action-plan").PlanBundle): Promise<import("./action-plan").PlanBundleResult> => {
  const { review_context, ...bundle } = body;
  if (!review_context) return fetchJson(`/api/projects/${encodeURIComponent(projectId)}/action-plan/bundles`, {
    method: "POST", body: JSON.stringify(bundle),
  });
  const result = await decideActionPlanReview(projectId, review_context.review_id, {
    decision_id: review_context.decision_id, expected_revision: review_context.expected_revision,
    decision: "approved", reason: bundle.reason, bundle,
  });
  if (result.review_id !== review_context.review_id || result.decision_id !== review_context.decision_id
      || result.status !== "approved" || !["applied", "duplicate"].includes(result.outcome) || !result.action_result) throw new Error("Mismatched action review receipt");
  return { ...result.action_result, outcome: result.outcome === "duplicate" ? "duplicate" : result.action_result.outcome };
};

// One frozen reporting snapshot for the page, clipboard image, and Markdown.
export const getProjectSummaryReport = (mine = false) =>
  fetchJson<import("./project-report").ProjectReport>(`/api/reports/project-summary?mine=${mine}`);

// Admin-only reminder receipts. Both actions record evidence; neither resends Telegram.
export type ReminderDeliveryReviewItem = import("./generated/api-schema").components["schemas"]["ReminderDeliveryReviewItem"];
export type LinkedItemReviewRequest = import("./generated/api-schema").components["schemas"]["LinkedItemReviewRequest"];
export type ReminderReconcileRequest = import("./generated/api-schema").components["schemas"]["ReconcileReminderRequest"];
export type LinkedItemReviewResolution = import("./generated/api-schema").components["schemas"]["LinkedItemReviewResolution"];
export type ReminderDeliveryResolution = import("./generated/api-schema").components["schemas"]["ReminderDeliveryResolution"];

export const getReviewableReminderDeliveries = () =>
  fetchJson<ReminderDeliveryReviewItem[]>("/api/personal-reminder-deliveries/review");

export const reviewReminderLinkedItem = (runId: string, body: LinkedItemReviewRequest) =>
  fetchJson<LinkedItemReviewResolution>(
    `/api/personal-reminder-deliveries/${encodeURIComponent(runId)}/linked-item-review`,
    { method: "POST", body: JSON.stringify(body) },
  );

export const reconcileReminderDelivery = (runId: string, body: ReminderReconcileRequest) =>
  fetchJson<ReminderDeliveryResolution>(
    `/api/personal-reminder-deliveries/${encodeURIComponent(runId)}/reconcile`,
    { method: "POST", body: JSON.stringify(body) },
  );

// Admin-only, evidence-bound reconciliation of uncertain outbound draft email.
export type DraftOutboxReconciliationItem = import("./generated/api-schema").components["schemas"]["OutboxReviewItem"];
export type DraftOutboxReconcileRequest = Omit<
  import("./generated/api-schema").components["schemas"]["ReconcileDraftOutboxRequest"],
  "full_recipient_set_verified"
> & { full_recipient_set_verified: true };
export type DraftOutboxReconcileResult = import("./generated/api-schema").components["schemas"]["OutboxReconcileResult"];
export const getDraftOutboxReconciliation = () =>
  fetchJson<DraftOutboxReconciliationItem[]>("/api/draft-outbox/reconciliation");

export const reconcileDraftOutbox = (outboxId: string, body: DraftOutboxReconcileRequest) =>
  fetchJson<DraftOutboxReconcileResult>(`/api/draft-outbox/${encodeURIComponent(outboxId)}/reconcile`, {
    method: "POST", body: JSON.stringify(body),
  });
