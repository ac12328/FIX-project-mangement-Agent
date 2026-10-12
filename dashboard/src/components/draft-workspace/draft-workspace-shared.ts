import { ApiError } from "@/lib/api";
import type {
  DraftWorkspace,
  WorkspaceDraft,
  WorkspaceEnvelope,
  WorkspaceLeaseSummary,
} from "@/lib/draft-workspace-api";

export interface WorkingForm {
  subject: string;
  bodyHtml: string;
  envelope: WorkspaceEnvelope;
}

export type SaveState = "saved" | "unsaved" | "saving" | "conflict" | "error";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isWorkspaceDraft(value: unknown): value is WorkspaceDraft {
  if (!isRecord(value)) return false;
  const content = value.content;
  const deliveryContext = value.delivery_context;
  const collaboration = value.collaboration;
  const revision = value.revision;

  return (
    typeof value.draft_id === "string" &&
    isRecord(content) &&
    typeof content.subject === "string" &&
    typeof content.body_html === "string" &&
    isRecord(value.envelope) &&
    isRecord(deliveryContext) &&
    typeof deliveryContext.delivery_mode === "string" &&
    isRecord(collaboration) &&
    typeof collaboration.review_state === "string" &&
    isRecord(revision) &&
    typeof revision.number === "number" &&
    typeof value.delivery_status === "string"
  );
}

function isWorkspaceLeaseSummary(value: unknown): value is WorkspaceLeaseSummary {
  return (
    isRecord(value) &&
    typeof value.draft_id === "string" &&
    typeof value.holder_user_id === "string" &&
    typeof value.holder_display_name === "string" &&
    typeof value.expires_at === "string" &&
    typeof value.updated_at === "string"
  );
}

function conflictPayload(error: unknown): Record<string, unknown> | null {
  if (!(error instanceof ApiError) || error.status !== 409 || !isRecord(error.detail)) {
    return null;
  }
  return error.detail;
}

export function formFromWorkspace(workspace: DraftWorkspace): WorkingForm {
  return {
    subject: workspace.draft.content.subject,
    bodyHtml: workspace.draft.content.body_html,
    envelope: workspace.draft.envelope,
  };
}

export function deliveryLocked(draft: WorkspaceDraft): boolean {
  return (
    ["scheduled", "undoable", "sending", "sent"].includes(draft.delivery_status) ||
    draft.collaboration.review_state === "requested"
  );
}

export function formatDate(value: string | null): string {
  if (!value) return "Not set";
  const date = new Date(value);
  return Number.isNaN(date.valueOf())
    ? value
    : new Intl.DateTimeFormat(undefined, {
        dateStyle: "medium",
        timeStyle: "short",
      }).format(date);
}

export function statusTone(status: string): string {
  if (["sent", "approved"].includes(status)) {
    return "border-success/30 bg-success/10 text-success";
  }
  if (["failed", "needs_attention", "stale", "uncertain"].includes(status)) {
    return "border-danger/30 bg-danger/10 text-danger";
  }
  if (["scheduled", "undoable", "requested", "changes_requested"].includes(status)) {
    return "border-warning/30 bg-warning/10 text-warning";
  }
  return "border-info/30 bg-info/10 text-info";
}

export function conflictDetail(error: unknown): WorkspaceDraft | null {
  const latest = conflictPayload(error)?.latest;
  return isWorkspaceDraft(latest) ? latest : null;
}

export function leaseDetail(error: unknown): WorkspaceLeaseSummary | null {
  const lease = conflictPayload(error)?.lease;
  return isWorkspaceLeaseSummary(lease) ? lease : null;
}
