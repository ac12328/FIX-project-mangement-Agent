import { fetchJson, LLM_TIMEOUT_MS } from "./api";
import type { BallWith, TicketStatus } from "./enums";
import type { ChatHistoryResponse, ChatMessage, ChatSendResponse } from "./chat-types";

// ── Types ────────────────────────────────────────────────────────

export interface SupportTicket {
  ticket_id: string;
  title: string;
  category: string;
  severity: string;
  status: TicketStatus | string;
  source_type?: string;
  related_project_id?: string;
  counterparty?: string;
  counterparty_org_id?: string;
  counterparty_org_name?: string;
  reporter_email?: string;
  description?: string;
  root_cause?: string;
  resolution?: string;
  assigned_team?: string;
  escalated_to?: string;
  resolution_pattern_id?: string;
  reply_draft_text?: string;
  draft_generated_at?: string;
  created_at?: string;
  updated_at?: string;
}

export interface TicketEvidence {
  evidence_id: string;
  ticket_id: string;
  evidence_type: string;
  content_summary: string;
  content_text?: string;
  source_ref?: string;
  created_at?: string;
}

// ── API calls ────────────────────────────────────────────────────

export async function getSupportTickets(params?: {
  status?: string;
  query?: string;
  limit?: number;
  offset?: number;
}): Promise<{ tickets: SupportTicket[]; total: number }> {
  const qs = new URLSearchParams();
  if (params?.status) qs.set("status", params.status);
  if (params?.query) qs.set("query", params.query);
  if (params?.limit) qs.set("limit", String(params.limit));
  if (params?.offset) qs.set("offset", String(params.offset));
  const q = qs.toString();
  return fetchJson(`/api/support${q ? `?${q}` : ""}`);
}

export async function getSupportTicket(ticketId: string): Promise<SupportTicket> {
  return fetchJson(`/api/support/${ticketId}`);
}

export async function updateSupportTicket(
  ticketId: string,
  updates: Partial<SupportTicket>,
): Promise<{ success: boolean; ticket_id: string; updated_fields: string[] }> {
  return fetchJson(`/api/support/${ticketId}`, {
    method: "PATCH",
    body: JSON.stringify(updates),
  });
}

export async function getTicketEvidence(
  ticketId: string,
): Promise<{ evidence: TicketEvidence[]; total: number }> {
  return fetchJson(`/api/support/${ticketId}/evidence`);
}

export interface TicketAISummary {
  ticket_id: string;
  from_party: string;
  to_party: string;
  what_happened: string;
  reply_draft_text?: string;
  recommended_action?: "send_reply" | "close_directly" | "investigate";
}

export async function getSupportSummary(ticketId: string): Promise<TicketAISummary> {
  return fetchJson(`/api/support/${ticketId}/summary`, { timeoutMs: LLM_TIMEOUT_MS });
}

export async function postSupportAction(
  ticketId: string,
  actionType: string,
  params: Record<string, string> = {}
): Promise<{ success: boolean; [key: string]: unknown }> {
  return fetchJson(`/api/support/${ticketId}/action`, {
    method: "POST",
    body: JSON.stringify({ action_type: actionType, params }),
  });
}

export async function getChatHistory(
  ticketId: string,
): Promise<ChatHistoryResponse> {
  return fetchJson(`/api/support/${ticketId}/chat`);
}

export async function sendChatMessage(
  ticketId: string,
  message: string,
  sessionMessages: ChatMessage[] = [],
): Promise<ChatSendResponse> {
  return fetchJson(`/api/support/${ticketId}/chat`, {
    method: "POST",
    body: JSON.stringify({ message, session_messages: sessionMessages }),
    timeoutMs: LLM_TIMEOUT_MS,
  });
}

export async function createTicketDraft(
  ticketId: string,
  bodyText?: string,
): Promise<{ draft_id: string }> {
  return fetchJson(`/api/support/${ticketId}/create-draft`, {
    method: "POST",
    ...(bodyText !== undefined && { body: JSON.stringify({ body_text: bodyText }) }),
  });
}

// ── Enriched Detail Types ───────────────────────────────────────

export interface ConversationMessage {
  evidence_id: string;
  direction: "inbound" | "outbound" | "internal";
  sender_email: string | null;
  sender_name: string | null;
  subject: string | null;
  preview: string;
  content_text: string | null;
  timestamp: string;
  evidence_type: string;
  is_latest: boolean;
}

export interface BallState {
  ball_with: BallWith;
  since: string | null;
  last_outbound_summary: string | null;
  ball_reason?: string | null;
}

export interface FollowUpInfo {
  has_followup: boolean;
  followup_id?: string;
  due_date?: string;
  description?: string;
}

export interface ActivityEntry {
  timestamp: string;
  actor: string;
  event_type: string;
  summary: string;
  dot_color: "blue" | "purple" | "amber" | "gray";
}

export interface ProjectLinkInfo {
  has_project: boolean;
  project_id?: string;
  project_name?: string;
  counterparty?: string;
  current_milestone?: string;
}

export interface TicketDetail {
  ticket_id: string;
  title: string;
  category: string;
  severity: string;
  status: TicketStatus | string;
  source_type?: string;
  counterparty?: string;
  counterparty_org_id?: string;
  counterparty_org_name?: string;
  reporter_email?: string;
  assigned_team?: string;
  escalated_to?: string;
  description?: string;
  root_cause?: string;
  resolution?: string;
  created_at?: string;
  updated_at?: string;
  ball: BallState;
  conversation: ConversationMessage[];
  followup: FollowUpInfo;
  activity: ActivityEntry[];
  project: ProjectLinkInfo;
  ai_summary?: TicketAISummary | null;
  reply_draft_text?: string;
}

export async function getTicketDetail(ticketId: string): Promise<TicketDetail> {
  return fetchJson(`/api/support/${ticketId}/detail`);
}

export async function addTicketNote(
  ticketId: string,
  text: string,
): Promise<{ success: boolean }> {
  return fetchJson(`/api/support/${ticketId}/action`, {
    method: "POST",
    body: JSON.stringify({ action_type: "add_note", params: { text } }),
  });
}

export async function bustAllSummaryCaches(): Promise<{ cleared: number }> {
  return fetchJson("/api/support/bust-summary-cache", { method: "POST" });
}
