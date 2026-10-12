import { API_BASE } from "./api-base";
import { fetchJson } from "./api";
import type {
  AIProposal,
  CheckpointComparison,
  DraftWorkspace,
  FinalPreview,
  PreflightResult,
  WorkspaceAttachment,
  WorkspaceCheckpoint,
  WorkspaceComment,
  WorkspaceDraft,
  WorkspaceLease,
  WorkspaceOutbox,
  WorkspacePatch,
} from "./draft-workspace-types";

export * from "./draft-workspace-types";

export const getDraftWorkspace = (draftId: string) =>
  fetchJson<DraftWorkspace>(`/api/drafts/${draftId}/workspace`, { timeoutMs: 20_000 });

export const patchDraftWorkspace = (
  draftId: string,
  expectedRevision: number,
  patch: WorkspacePatch,
  leaseToken?: string | null,
) =>
  fetchJson<DraftWorkspace>(`/api/drafts/${draftId}/workspace`, {
    method: "PATCH",
    body: JSON.stringify({ expected_revision: expectedRevision, lease_token: leaseToken, patch }),
    timeoutMs: 20_000,
  });

export const confirmNewEmail = (draftId: string, expectedRevision: number, leaseToken?: string | null) =>
  fetchJson<DraftWorkspace>(`/api/drafts/${draftId}/workspace/delivery/new`, {
    method: "POST",
    body: JSON.stringify({ expected_revision: expectedRevision, lease_token: leaseToken }),
  });

export const confirmReplyThread = (
  draftId: string,
  expectedRevision: number,
  emailId: string,
  leaseToken?: string | null,
) =>
  fetchJson<DraftWorkspace>(`/api/drafts/${draftId}/workspace/delivery/reply`, {
    method: "POST",
    body: JSON.stringify({ expected_revision: expectedRevision, lease_token: leaseToken, email_id: emailId }),
  });

export const acquireDraftLease = (draftId: string) =>
  fetchJson<WorkspaceLease>(`/api/drafts/${draftId}/workspace/lease/acquire`, { method: "POST", body: "{}" });

export const renewDraftLease = (draftId: string, leaseToken: string) =>
  fetchJson<WorkspaceLease>(`/api/drafts/${draftId}/workspace/lease/renew`, {
    method: "POST",
    body: JSON.stringify({ lease_token: leaseToken }),
  });

export const releaseDraftLease = (draftId: string, leaseToken: string, keepalive = false) =>
  fetchJson<{ released: boolean }>(`/api/drafts/${draftId}/workspace/lease/release`, {
    method: "POST",
    body: JSON.stringify({ lease_token: leaseToken }),
    keepalive,
  });

export const takeOverDraftLease = (draftId: string) =>
  fetchJson<WorkspaceLease>(`/api/drafts/${draftId}/workspace/lease/takeover`, { method: "POST", body: "{}" });

export const requestDraftReview = (draftId: string, reviewerUserId: string) =>
  fetchJson<WorkspaceDraft>(`/api/drafts/${draftId}/workspace/review/request`, {
    method: "POST",
    body: JSON.stringify({ reviewer_user_id: reviewerUserId }),
  });

export const approveDraftReview = (draftId: string) =>
  fetchJson<WorkspaceDraft>(`/api/drafts/${draftId}/workspace/review/approve`, { method: "POST", body: "{}" });

export const requestDraftChanges = (draftId: string) =>
  fetchJson<WorkspaceDraft>(`/api/drafts/${draftId}/workspace/review/changes`, { method: "POST", body: "{}" });

export const cancelDraftReview = (draftId: string) =>
  fetchJson<WorkspaceDraft>(`/api/drafts/${draftId}/workspace/review/cancel`, { method: "POST", body: "{}" });

export const createDraftComment = (
  draftId: string,
  payload: {
    body: string;
    location_type?: WorkspaceComment["location_type"];
    location_ref?: string | null;
    parent_comment_id?: string | null;
  },
) =>
  fetchJson<WorkspaceComment>(`/api/drafts/${draftId}/workspace/comments`, {
    method: "POST",
    body: JSON.stringify(payload),
  });

export const setDraftCommentResolved = (draftId: string, commentId: string, resolved: boolean) =>
  fetchJson<WorkspaceComment>(`/api/drafts/${draftId}/workspace/comments/${commentId}`, {
    method: "PATCH",
    body: JSON.stringify({ resolved }),
  });

export const createDraftCheckpoint = (draftId: string, reason = "manual") =>
  fetchJson<WorkspaceCheckpoint>(`/api/drafts/${draftId}/workspace/checkpoints`, {
    method: "POST",
    body: JSON.stringify({ reason }),
  });

export const compareDraftCheckpoint = (draftId: string, version: number) =>
  fetchJson<CheckpointComparison>(`/api/drafts/${draftId}/workspace/checkpoints/${version}/compare`);

export const restoreDraftCheckpoint = (
  draftId: string,
  version: number,
  expectedRevision: number,
  leaseToken?: string | null,
) =>
  fetchJson<DraftWorkspace>(`/api/drafts/${draftId}/workspace/checkpoints/${version}/restore`, {
    method: "POST",
    body: JSON.stringify({ expected_revision: expectedRevision, lease_token: leaseToken }),
  });

export const createAIProposal = (
  draftId: string,
  payload: {
    action: AIProposal["action"];
    proposal: AIProposal["proposal"];
    base_revision?: number;
    base_hash?: string;
  },
) =>
  fetchJson<AIProposal>(`/api/drafts/${draftId}/workspace/ai-proposals`, {
    method: "POST",
    body: JSON.stringify(payload),
    timeoutMs: 60_000,
  });

export const applyAIProposal = (
  draftId: string,
  proposalId: string,
  selectedChanges?: string[],
  leaseToken?: string | null,
) =>
  fetchJson<DraftWorkspace>(`/api/drafts/${draftId}/workspace/ai-proposals/${proposalId}/apply`, {
    method: "POST",
    body: JSON.stringify({ selected_changes: selectedChanges, lease_token: leaseToken }),
  });

export const rejectAIProposal = (draftId: string, proposalId: string) =>
  fetchJson<AIProposal>(`/api/drafts/${draftId}/workspace/ai-proposals/${proposalId}/reject`, {
    method: "POST",
    body: "{}",
  });

export const getDraftPreflight = (draftId: string) =>
  fetchJson<PreflightResult>(`/api/drafts/${draftId}/workspace/preflight`);

export const getDraftPreview = (draftId: string) =>
  fetchJson<FinalPreview>(`/api/drafts/${draftId}/workspace/preview`);

export const sendDraftNow = (
  draftId: string,
  expectedRevision: number,
  expectedHash: string,
  confirmedWarnings: string[],
) =>
  fetchJson<WorkspaceOutbox>(`/api/drafts/${draftId}/workspace/send-now`, {
    method: "POST",
    body: JSON.stringify({
      expected_revision: expectedRevision,
      expected_hash: expectedHash,
      confirmed_warnings: confirmedWarnings,
    }),
    timeoutMs: 90_000,
  });

export const sendDraftLater = (
  draftId: string,
  expectedRevision: number,
  expectedHash: string,
  confirmedWarnings: string[],
  scheduledFor: string,
) =>
  fetchJson<WorkspaceOutbox>(`/api/drafts/${draftId}/workspace/send-later`, {
    method: "POST",
    body: JSON.stringify({
      expected_revision: expectedRevision,
      expected_hash: expectedHash,
      confirmed_warnings: confirmedWarnings,
      scheduled_for: scheduledFor,
    }),
    timeoutMs: 90_000,
  });

export const undoDraftSend = (draftId: string, outboxId: string) =>
  fetchJson<WorkspaceOutbox>(`/api/drafts/${draftId}/workspace/outbox/${outboxId}/undo`, {
    method: "POST",
    body: "{}",
  });

export const cancelScheduledDraft = (draftId: string, outboxId: string) =>
  fetchJson<WorkspaceOutbox>(`/api/drafts/${draftId}/workspace/outbox/${outboxId}/cancel`, {
    method: "POST",
    body: "{}",
  });

export const rescheduleDraft = (draftId: string, outboxId: string, scheduledFor: string) =>
  fetchJson<WorkspaceOutbox>(`/api/drafts/${draftId}/workspace/outbox/${outboxId}/reschedule`, {
    method: "POST",
    body: JSON.stringify({ scheduled_for: scheduledFor }),
  });

export const getRetryDraftPreview = (draftId: string, outboxId: string) =>
  fetchJson<PreflightResult>(`/api/drafts/${draftId}/workspace/outbox/${outboxId}/preview`);

export const retryDraftSend = (draftId: string, outboxId: string, confirmedWarnings: string[]) =>
  fetchJson<WorkspaceOutbox>(`/api/drafts/${draftId}/workspace/outbox/${outboxId}/retry`, {
    method: "POST",
    body: JSON.stringify({ confirmed_warnings: confirmedWarnings }),
  });

export const uploadWorkspaceAttachment = (
  draftId: string,
  file: File,
  expectedRevision: number,
  leaseToken?: string | null,
) => {
  const data = new FormData();
  data.append("file", file);
  data.append("expected_revision", String(expectedRevision));
  if (leaseToken) data.append("lease_token", leaseToken);
  return fetchJson<{ attachment: WorkspaceAttachment; workspace: DraftWorkspace }>(
    `/api/drafts/${draftId}/attachments`,
    { method: "POST", body: data, timeoutMs: 90_000 },
  );
};

export const deleteWorkspaceAttachment = (
  draftId: string,
  filename: string,
  expectedRevision: number,
  leaseToken?: string | null,
) => {
  const params = new URLSearchParams({ expected_revision: String(expectedRevision) });
  if (leaseToken) params.set("lease_token", leaseToken);
  return fetchJson<{ success: boolean; workspace: DraftWorkspace }>(
    `/api/drafts/${draftId}/attachments/${encodeURIComponent(filename)}?${params.toString()}`,
    { method: "DELETE" },
  );
};

export const workspaceAttachmentDownloadUrl = (draftId: string, filename: string) =>
  `${API_BASE}/api/drafts/${draftId}/attachments/${encodeURIComponent(filename)}/download`;
