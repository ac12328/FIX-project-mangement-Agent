export type DeliveryMode = "unresolved" | "new" | "reply";
export type ReviewState =
  | "not_requested"
  | "requested"
  | "changes_requested"
  | "approved"
  | "cancelled"
  | "stale";
export type DeliveryStatus =
  | "editing"
  | "scheduled"
  | "undoable"
  | "sending"
  | "sent"
  | "failed"
  | "cancelled"
  | "needs_attention";
export type RecipientSource = "project_contact" | "thread_participant" | "manual" | string;

export interface WorkspaceRecipient {
  email: string;
  display_name: string | null;
  contact_id: string | null;
  source: RecipientSource;
}

export interface WorkspaceEnvelope {
  sender_identity_id: string | null;
  to: WorkspaceRecipient[];
  cc: WorkspaceRecipient[];
  bcc: WorkspaceRecipient[];
}

export interface WorkspaceContent {
  subject: string;
  body_html: string;
  body_text: string;
}

export interface WorkspaceDeliveryContext {
  delivery_mode: DeliveryMode;
  reply_to_email_id: string | null;
  suggested_reply_to_email_id: string | null;
  context_packet_ref: string | null;
}

export interface WorkspaceCollaboration {
  owner_user_id: string | null;
  reviewer_user_id: string | null;
  review_state: ReviewState;
  review_requested_by: string | null;
  review_requested_at: string | null;
  approval_hash: string | null;
  approved_at: string | null;
}

export interface WorkspaceRevision {
  number: number;
  checkpoint_version: number;
  send_affecting_hash: string;
  attachment_hash: string;
}

export interface WorkspaceAttachment {
  filename: string;
  size: number;
  sha256: string | null;
}

export interface WorkspaceDraft {
  draft_id: string;
  project_id: string | null;
  task_type: string | null;
  content: WorkspaceContent;
  envelope: WorkspaceEnvelope;
  delivery_context: WorkspaceDeliveryContext;
  collaboration: WorkspaceCollaboration;
  revision: WorkspaceRevision;
  delivery_status: DeliveryStatus;
  legacy_status: string | null;
  attachments: WorkspaceAttachment[];
  created_at: string | null;
  updated_at: string | null;
}

export interface DeliveryCandidate {
  email_id: string;
  subject: string | null;
  sender_email: string | null;
  direction: string | null;
  received_at: string | null;
  processed_at: string | null;
  cc: string | null;
  to_recipients_json: string | null;
  cc_recipients_json: string | null;
  body_snippet: string | null;
  confirmed: boolean;
  suggested: boolean;
  is_evidence_source: boolean;
}

export interface WorkspaceContact {
  contact_id: string;
  display_name: string;
  primary_email: string;
  role: string | null;
  department: string | null;
  role_on_project: string | null;
  is_primary: number | boolean;
}

export interface SenderIdentity {
  identity_id: string;
  display_name: string;
  email_address: string;
  send_method: string;
  scope: string;
  project_id: string | null;
  is_default: boolean;
}

export interface PreflightIssue {
  code: string;
  message: string;
  field: string;
  severity: "blocker" | "warning";
  metadata: Record<string, unknown>;
}

export interface FinalPreview {
  from: SenderIdentity | null;
  to: WorkspaceRecipient[];
  cc: WorkspaceRecipient[];
  bcc: WorkspaceRecipient[];
  subject: string;
  body_html: string;
  body_text: string;
  attachments: WorkspaceAttachment[];
  delivery_mode: DeliveryMode;
  thread: Record<string, unknown> | null;
}

export interface PreflightResult {
  ok: boolean;
  snapshot_hash: string;
  revision: number;
  blockers: PreflightIssue[];
  warnings: PreflightIssue[];
  preview: FinalPreview;
}

export interface WorkspaceComment {
  comment_id: string;
  draft_id: string;
  parent_comment_id: string | null;
  author_user_id: string;
  author_display_name: string;
  location_type: "general" | "subject" | "recipient" | "body" | "attachment";
  location_ref: string | null;
  body: string;
  resolved_at: string | null;
  resolved_by: string | null;
  created_at: string;
  updated_at: string;
  replies?: WorkspaceComment[];
}

export interface WorkspaceCheckpoint {
  checkpoint_id: string;
  version: number;
  revision: number;
  snapshot_hash: string;
  reason: string;
  created_by: string;
  created_at: string;
}

export interface WorkspaceAuditEvent {
  event_id: string;
  event_type: string;
  actor_user_id: string;
  metadata: Record<string, unknown>;
  created_at: string;
}

export interface WorkspaceLeaseSummary {
  draft_id: string;
  holder_user_id: string;
  holder_display_name: string;
  expires_at: string;
  updated_at: string;
}

export interface WorkspaceLease extends WorkspaceLeaseSummary {
  lease_token: string;
}

export interface WorkspaceOutbox {
  outbox_id: string;
  draft_id: string;
  status: string;
  scheduled_for: string | null;
  undo_until: string | null;
  created_by: string;
  created_at: string;
  updated_at: string;
  sent_at: string | null;
  cancelled_at: string | null;
  provider_message_id: string | null;
  provider_result: Record<string, unknown> | null;
  last_error: string | null;
  attempt_count: number;
}

export interface AIProposal {
  proposal_id: string;
  draft_id: string;
  base_revision: number;
  base_hash: string;
  action: "rewrite" | "tone" | "shorten" | "translate" | "fact_check";
  proposal: Partial<WorkspaceContent> & Record<string, unknown>;
  status: "open" | "applied" | "rejected" | "stale";
  created_by: string;
  created_at: string;
  applied_at: string | null;
}

export interface DraftWorkspace {
  draft: WorkspaceDraft;
  delivery_candidates: DeliveryCandidate[];
  contacts: WorkspaceContact[];
  sender_identities: SenderIdentity[];
  attachments: WorkspaceAttachment[];
  review: WorkspaceCollaboration;
  comments: WorkspaceComment[];
  ai_proposals: AIProposal[];
  history: {
    checkpoints: WorkspaceCheckpoint[];
    audit: WorkspaceAuditEvent[];
    outbox: WorkspaceOutbox[];
  };
  lease: WorkspaceLeaseSummary | null;
  outbox: WorkspaceOutbox | null;
  capabilities: {
    attachment: {
      max_file_size: number;
      max_total_size: number;
      allowed_extensions: string[];
    };
    lease_seconds: number;
    lease_renew_seconds: number;
    undo_send_seconds: number;
    rich_text_tags: string[];
  };
  preflight: PreflightResult;
}

export interface WorkspacePatch {
  content?: Partial<WorkspaceContent>;
  envelope?: Partial<WorkspaceEnvelope>;
  delivery_context?: Partial<WorkspaceDeliveryContext>;
  collaboration?: Pick<WorkspaceCollaboration, "owner_user_id" | "reviewer_user_id">;
}

export interface CheckpointComparison {
  version: number;
  subject_before: string;
  subject_after: string;
  body_diff: string[];
  snapshot_before: WorkspaceDraft;
  snapshot_after: WorkspaceDraft;
}
