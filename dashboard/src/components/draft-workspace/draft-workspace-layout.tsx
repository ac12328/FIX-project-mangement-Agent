"use client";

import Link from "next/link";
import {
  AlertCircle, ArrowLeft, CalendarClock, CheckCircle2, ChevronDown, ChevronUp,
  Cloud, CloudOff, Eye, FileCheck2, LockKeyhole, RefreshCw, RotateCcw,
  Save, Send, ShieldCheck, UserRoundCheck, X,
} from "lucide-react";

import { AttachmentsPanel } from "@/components/draft-workspace/attachments-panel";
import { FinalPreviewDialog } from "@/components/draft-workspace/final-preview-dialog";
import { PreflightPanel } from "@/components/draft-workspace/preflight-panel";
import { RecipientField } from "@/components/draft-workspace/recipient-field";
import { RichTextEditor } from "@/components/draft-workspace/rich-text-editor";
import { WorkspaceDialog } from "@/components/draft-workspace/workspace-dialog";
import { WorkspaceSidePanel } from "@/components/draft-workspace/workspace-side-panel";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import {
  deliveryLocked, formatDate, statusTone, type SaveState, type WorkingForm,
} from "@/components/draft-workspace/draft-workspace-shared";
import type {
  AIProposal, DraftWorkspace, PreflightResult, WorkspaceDraft, WorkspaceLeaseSummary,
} from "@/lib/draft-workspace-api";
import type { MemoryItem, ProjectDetail } from "@/lib/types";
import { cn } from "@/lib/utils";

interface DraftWorkspaceLayoutProps {
  workspace: DraftWorkspace | null;
  form: WorkingForm | null;
  project: ProjectDetail | null;
  memory: MemoryItem[];
  sessionUserId?: string;
  loadingError: string | null;
  actionError: string | null;
  notice: string | null;
  dirty: boolean;
  saveState: SaveState;
  leaseToken: string | null;
  leaseConflict: WorkspaceLeaseSummary | null;
  busyAction: string | null;
  showBcc: boolean;
  preview: PreflightResult | null;
  previewOpen: boolean;
  retryMode: boolean;
  conflict: WorkspaceDraft | null;
  reviewerUserId: string;
  rescheduleValue: string;
  undoSeconds: number;
  flushAutosave: () => Promise<DraftWorkspace>;
  onWorkspaceChange: (workspace: DraftWorkspace, resetForm?: boolean) => void;
  onLoad: () => Promise<void>;
  onAcquireLease: () => Promise<void>;
  onUpdateForm: (updater: (current: WorkingForm) => WorkingForm) => void;
  onSave: () => Promise<void>;
  onCheckpoint: () => Promise<void>;
  onTakeover: () => Promise<void>;
  onConfirmNew: () => Promise<void>;
  onConfirmReply: (emailId: string) => Promise<void>;
  onRestore: (version: number) => Promise<void>;
  onApplyProposal: (proposal: AIProposal, selectedChanges?: string[]) => Promise<void>;
  onRejectProposal: (proposal: AIProposal) => Promise<void>;
  onRefreshWorkspace: () => Promise<void>;
  onRefreshPreflight: () => Promise<PreflightResult | null>;
  onOpenFinalPreview: () => Promise<void>;
  onOpenRetryPreview: () => Promise<void>;
  onSendNow: (confirmedWarnings: string[]) => Promise<void>;
  onSendLater: (confirmedWarnings: string[], scheduledFor: string) => Promise<void>;
  onUndo: () => Promise<void>;
  onCancelSchedule: () => Promise<void>;
  onReschedule: () => Promise<void>;
  onRequestReview: () => Promise<void>;
  onReviewAction: (action: "approve" | "changes" | "cancel") => Promise<void>;
  onSetPreviewOpen: (open: boolean) => void;
  onSetActionError: (message: string | null) => void;
  onSetShowBcc: (value: boolean | ((current: boolean) => boolean)) => void;
  onSetReviewerUserId: (value: string) => void;
  onSetRescheduleValue: (value: string) => void;
  onReloadConflict: () => Promise<void>;
  onReapplyConflict: () => Promise<void>;
}

export function DraftWorkspaceLayout(props: DraftWorkspaceLayoutProps) {
  const {
    workspace, form, project, memory, sessionUserId, loadingError, actionError,
    notice, dirty, saveState, leaseToken, leaseConflict, busyAction, showBcc,
    preview, previewOpen, retryMode, conflict, reviewerUserId, rescheduleValue,
    undoSeconds, flushAutosave, onWorkspaceChange: installWorkspace,
    onLoad: load, onAcquireLease: acquireLease, onUpdateForm: updateForm,
    onSave: handleSave, onCheckpoint: handleCheckpoint, onTakeover: handleTakeover,
    onConfirmNew: handleConfirmNew, onConfirmReply: handleConfirmReply,
    onRestore: handleRestore, onApplyProposal: handleApplyProposal,
    onRejectProposal: handleRejectProposal, onRefreshWorkspace: refreshWorkspace,
    onRefreshPreflight: refreshPreflight, onOpenFinalPreview: openFinalPreview,
    onOpenRetryPreview: openRetryPreview, onSendNow: handleSendNow,
    onSendLater: handleSendLater, onUndo: handleUndo,
    onCancelSchedule: handleCancelSchedule, onReschedule: handleReschedule,
    onRequestReview: handleRequestReview, onReviewAction: handleReviewAction,
    onSetPreviewOpen: setPreviewOpen, onSetActionError: setActionError,
    onSetShowBcc: setShowBcc, onSetReviewerUserId: setReviewerUserId,
    onSetRescheduleValue: setRescheduleValue, onReloadConflict: reloadConflict,
    onReapplyConflict: reapplyConflict,
  } = props;
  const session = sessionUserId ? { user_id: sessionUserId } : null;
  const flushAutosaveRef = { current: flushAutosave };

  const focusField = (field: string) => {
    const id = field.includes("sender_identity") ? "sender-identity"
      : field.includes("envelope.to") || field === "to" ? "recipients-to"
        : field.includes("envelope.cc") ? "recipients-cc"
          : field.includes("envelope.bcc") ? "recipients-bcc"
            : field.includes("subject") ? "draft-subject"
              : field.includes("body") ? "draft-body"
                : field.includes("attachment") ? "attachments"
                  : field.includes("delivery_context") ? "delivery-context"
                    : field.includes("review") ? "review" : "preflight";
    const element = document.getElementById(id);
    element?.scrollIntoView({ behavior: "smooth", block: "center" });
    const focusable = element?.matches("input,select,textarea,button,[contenteditable='true']")
      ? element
      : element?.querySelector<HTMLElement>("input,select,textarea,button,[contenteditable='true']");
    window.setTimeout(() => (focusable as HTMLElement | null)?.focus(), 250);
  };

  if (loadingError && !workspace) {
    return (
      <div className="mx-auto max-w-3xl py-10">
        <Card className="border border-danger/25 shadow-sm">
          <CardContent className="flex flex-col items-center gap-4 py-12 text-center">
            <span className="grid size-12 place-items-center rounded-full bg-danger/10 text-danger">
              <AlertCircle className="size-6" />
            </span>
            <div>
              <h1 className="font-heading text-xl font-semibold">Draft workspace unavailable</h1>
              <p className="mt-1 text-sm text-muted-foreground">{loadingError}</p>
            </div>
            <div className="flex flex-wrap justify-center gap-2">
              <Button type="button" onClick={() => void load()}><RefreshCw className="size-4" /> Try again</Button>
              <Button render={<Link href="/mail?tab=drafts" />} variant="outline"><ArrowLeft className="size-4" /> Back to drafts</Button>
            </div>
          </CardContent>
        </Card>
      </div>
    );
  }

  if (!workspace || !form) {
    return (
      <div className="mx-auto max-w-[96rem] space-y-5" aria-busy="true" aria-label="Loading draft workspace">
        <div className="flex items-center justify-between gap-4">
          <div className="space-y-2"><Skeleton className="h-4 w-32" /><Skeleton className="h-9 w-80 max-w-[70vw]" /></div>
          <Skeleton className="h-11 w-40" />
        </div>
        <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_minmax(22rem,28rem)]">
          <div className="space-y-5"><Skeleton className="h-44 w-full rounded-xl" /><Skeleton className="h-[34rem] w-full rounded-xl" /></div>
          <Skeleton className="h-[42rem] w-full rounded-xl" />
        </div>
      </div>
    );
  }

  const draft = workspace.draft;
  const review = draft.collaboration;
  const outbox = workspace.outbox;
  const deliveryHistory = workspace.history.outbox ?? [];
  const uncertainDeliveries = deliveryHistory.filter((item) => item.status === "uncertain");
  const reviewState = review.review_state;
  const lockedByDelivery = deliveryLocked(draft);
  const readOnly = lockedByDelivery || !leaseToken;
  const retryable = Boolean(outbox && ["failed", "needs_attention"].includes(outbox.status));
  const preflightStale = dirty || workspace.preflight.revision !== draft.revision.number;
  const allIssues = [...workspace.preflight.blockers, ...workspace.preflight.warnings];
  const issueFor = (...tokens: string[]) => allIssues.find((issue) => tokens.some((token) => issue.field.includes(token)));
  const evidence = workspace.delivery_candidates.find((candidate) => candidate.is_evidence_source);
  const confirmedThread = workspace.delivery_candidates.find((candidate) => candidate.confirmed);
  const suggestedThread = workspace.delivery_candidates.find((candidate) => candidate.suggested && !candidate.confirmed);
  const userIsReviewer = Boolean(session?.user_id && review.reviewer_user_id === session.user_id);
  const userCanCancelReview = Boolean(session?.user_id && [review.owner_user_id, review.review_requested_by].includes(session.user_id));
  const saveLabel = saveState === "saving" ? "Saving..."
    : saveState === "unsaved" ? "Unsaved changes"
      : saveState === "conflict" ? "Revision conflict"
        : saveState === "error" ? "Save failed" : "All changes saved";
  const SaveIcon = saveState === "conflict" || saveState === "error" ? CloudOff
    : saveState === "saved" ? CheckCircle2 : Cloud;

  return (
    <div className="mx-auto max-w-[96rem] space-y-5 pb-24 xl:pb-6">
      <header className="space-y-4">
        <Link href="/mail?tab=drafts" className="inline-flex min-h-10 items-center gap-2 rounded-lg px-1 text-sm font-medium text-muted-foreground outline-none transition-colors hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50">
          <ArrowLeft className="size-4" /> Back to drafts
        </Link>
        <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="truncate font-heading text-2xl font-semibold tracking-tight sm:text-3xl">
                {form.subject || "Untitled email"}
              </h1>
              <Badge variant="outline" className={statusTone(draft.delivery_status)}>{draft.delivery_status.replaceAll("_", " ")}</Badge>
              <Badge variant="outline" className={statusTone(reviewState)}>{reviewState.replaceAll("_", " ")}</Badge>
            </div>
            <p className="mt-1 text-sm text-muted-foreground">
              {project?.client_name || draft.project_id || "Unlinked draft"} / Revision {draft.revision.number} / Checkpoint {draft.revision.checkpoint_version}
            </p>
            <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-2 text-xs text-muted-foreground">
              <span className="inline-flex items-center gap-1.5"><UserRoundCheck className="size-3.5" /> Owner: {review.owner_user_id || "Unassigned"}</span>
              <span className="inline-flex items-center gap-1.5"><FileCheck2 className="size-3.5" /> Reviewer: {review.reviewer_user_id || "Not requested"}</span>
              <span className={cn("inline-flex items-center gap-1.5", saveState === "error" || saveState === "conflict" ? "text-danger" : saveState === "unsaved" ? "text-warning" : "text-muted-foreground")} aria-live="polite">
                <SaveIcon className={cn("size-3.5", saveState === "saving" && "animate-pulse")} /> {saveLabel}
              </span>
            </div>
          </div>
          <div className="hidden flex-wrap items-center justify-end gap-2 xl:flex">
            <Button type="button" variant="outline" disabled={readOnly || !dirty || saveState === "saving"} onClick={() => void handleSave()}>
              <Save className="size-4" /> Save now
            </Button>
            <Button type="button" variant="outline" disabled={readOnly || Boolean(busyAction)} onClick={() => void handleCheckpoint()}>
              <FileCheck2 className="size-4" /> Checkpoint
            </Button>
            <Button
              type="button"
              disabled={readOnly || Boolean(busyAction) || retryable}
              onClick={() => void openFinalPreview()}
            >
              {busyAction === "preflight" ? <RefreshCw className="size-4 animate-spin" /> : <Eye className="size-4" />}
              Preview &amp; send
            </Button>
          </div>
        </div>
      </header>

      <div className="sr-only" aria-live="polite" aria-atomic="true">{notice || actionError || saveLabel}</div>

      {leaseConflict && !lockedByDelivery && (
        <div className="flex flex-col gap-3 rounded-xl border border-warning/35 bg-warning/8 p-4 sm:flex-row sm:items-center" role="alert">
          <LockKeyhole className="size-5 shrink-0 text-warning" />
          <div className="min-w-0 flex-1">
            <p className="font-medium">Read-only while {leaseConflict.holder_display_name} is editing</p>
            <p className="mt-0.5 text-xs text-muted-foreground">Lease expires {formatDate(leaseConflict.expires_at)}. Comments remain available.</p>
          </div>
          <Button type="button" variant="outline" disabled={busyAction === "takeover"} onClick={() => void handleTakeover()}>
            <LockKeyhole className="size-4" /> {busyAction === "takeover" ? "Taking over..." : "Take over editing"}
          </Button>
        </div>
      )}

      {!leaseToken && !leaseConflict && !lockedByDelivery && (
        <div className="flex flex-col gap-3 rounded-xl border border-info/30 bg-info/8 p-4 sm:flex-row sm:items-center" role="status">
          <LockKeyhole className="size-5 shrink-0 text-info" />
          <div className="min-w-0 flex-1"><p className="font-medium">Editing access is not active</p><p className="mt-0.5 text-xs text-muted-foreground">Acquire the short-lived edit lease before changing this draft.</p></div>
          <Button type="button" variant="outline" onClick={() => void acquireLease()}>Acquire edit access</Button>
        </div>
      )}

      {actionError && (
        <div className="flex items-start gap-2 rounded-xl border border-danger/30 bg-danger/8 p-4 text-sm text-danger" role="alert">
          <AlertCircle className="mt-0.5 size-4 shrink-0" /><span className="min-w-0 flex-1">{actionError}</span>
          <Button type="button" size="icon-xs" variant="ghost" aria-label="Dismiss error" onClick={() => setActionError(null)}><X className="size-3.5" /></Button>
        </div>
      )}
      {notice && (
        <div className="flex items-start gap-2 rounded-xl border border-success/30 bg-success/8 p-4 text-sm" role="status">
          <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-success" /><span>{notice}</span>
        </div>
      )}

      {outbox && (
        <Card size="sm" className={cn("border shadow-sm", outbox.status === "undoable" ? "border-warning/40 bg-warning/5" : retryable ? "border-danger/35 bg-danger/5" : "border-border/80")}>
          <CardContent className="flex flex-col gap-4 pt-1 md:flex-row md:items-center">
            <span className={cn("grid size-11 shrink-0 place-items-center rounded-xl", outbox.status === "undoable" ? "bg-warning/15 text-warning" : retryable ? "bg-danger/10 text-danger" : "bg-info/10 text-info")}>
              {outbox.status === "undoable" ? <RotateCcw className="size-5" /> : outbox.status === "scheduled" ? <CalendarClock className="size-5" /> : retryable ? <AlertCircle className="size-5" /> : <Send className="size-5" />}
            </span>
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-2"><p className="font-semibold capitalize">Outbox: {outbox.status.replaceAll("_", " ")}</p><Badge variant="outline">Attempt {outbox.attempt_count}</Badge></div>
              {outbox.status === "undoable" && <p className="mt-1 text-sm text-muted-foreground">Delivery can be undone for {undoSeconds} more second{undoSeconds === 1 ? "" : "s"}.</p>}
              {outbox.scheduled_for && <p className="mt-1 text-sm text-muted-foreground">Scheduled for {formatDate(outbox.scheduled_for)} ({Intl.DateTimeFormat().resolvedOptions().timeZone}).</p>}
              {outbox.last_error && <p className="mt-1 break-words text-sm text-danger">{outbox.last_error}</p>}
              {outbox.sent_at && <p className="mt-1 text-sm text-muted-foreground">Sent {formatDate(outbox.sent_at)}.</p>}
            </div>
            {outbox.status === "undoable" && (
              <Button type="button" variant="outline" disabled={busyAction === "undo" || undoSeconds <= 0} onClick={() => void handleUndo()}>
                <RotateCcw className="size-4" /> {busyAction === "undo" ? "Undoing…" : `Undo (${undoSeconds}s)`}
              </Button>
            )}
            {outbox.status === "scheduled" && (
              <div className="flex w-full flex-col gap-2 md:w-auto md:min-w-72">
                <label htmlFor="reschedule-time" className="sr-only">New delivery time</label>
                <input id="reschedule-time" type="datetime-local" value={rescheduleValue} onChange={(event) => setRescheduleValue(event.target.value)} className="min-h-10 rounded-lg border bg-background px-3 text-sm outline-none focus:border-primary/60 focus:ring-3 focus:ring-primary/10" />
                <div className="flex gap-2">
                  <Button type="button" size="sm" variant="outline" className="flex-1" disabled={Boolean(busyAction)} onClick={() => void handleReschedule()}>Reschedule</Button>
                  <Button type="button" size="sm" variant="destructive" className="flex-1" disabled={Boolean(busyAction)} onClick={() => void handleCancelSchedule()}>Cancel</Button>
                </div>
              </div>
            )}
            {retryable && (
              <Button type="button" disabled={Boolean(busyAction)} onClick={() => void openRetryPreview()}>
                <RefreshCw className={cn("size-4", busyAction === "retry-preview" && "animate-spin")} /> Preview immutable retry
              </Button>
            )}
          </CardContent>
        </Card>
      )}

      {uncertainDeliveries.length > 0 && (
        <section className="rounded-xl border border-danger/40 bg-danger/8 p-4 text-sm" role="alert" aria-label="Unconfirmed email delivery">
          <div className="flex items-start gap-3">
            <AlertCircle className="mt-0.5 size-5 shrink-0 text-danger" aria-hidden="true" />
            <div className="space-y-2">
              <p className="font-semibold">Email delivery outcome unconfirmed</p>
              <p>One or more prior attempts may have reached recipients. Do not resend until an administrator reconciles the provider and recipient records.</p>
              <ul className="list-inside list-disc space-y-1 break-all text-xs text-muted-foreground">
                {uncertainDeliveries.map((item) => (
                  <li key={item.outbox_id}>{item.outbox_id} ? {formatDate(item.updated_at)}{item.last_error ? ` ? ${item.last_error}` : ""}</li>
                ))}
              </ul>
            </div>
          </div>
        </section>
      )}

      {deliveryHistory.length > 1 && (
        <Card size="sm" className="border border-border/80 shadow-sm">
          <CardHeader className="border-b"><CardTitle>Delivery history</CardTitle></CardHeader>
          <CardContent>
            <ol className="max-h-56 space-y-2 overflow-y-auto text-sm" aria-label="Previous delivery attempts">
              {deliveryHistory.map((item) => (
                <li key={item.outbox_id} className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg border border-border/70 p-2">
                  <Badge variant="outline" className={statusTone(item.status)}>{item.status.replaceAll("_", " ")}</Badge>
                  <span className="break-all font-mono text-xs">{item.outbox_id}</span>
                  <span className="text-xs text-muted-foreground">{formatDate(item.updated_at)}</span>
                  {item.last_error && <span className="w-full break-words text-xs text-muted-foreground">{item.last_error}</span>}
                </li>
              ))}
            </ol>
          </CardContent>
        </Card>
      )}

      <div className="grid items-start gap-5 xl:grid-cols-[minmax(0,1fr)_minmax(22rem,28rem)]">
        <main className="min-w-0 space-y-5">
          <Card size="sm" id="delivery-context" className="border border-border/80 shadow-sm">
            <CardHeader className="border-b"><CardTitle className="flex items-center gap-2"><ShieldCheck className="size-4 text-primary" /> Delivery context</CardTitle></CardHeader>
            <CardContent className="grid gap-4 lg:grid-cols-2">
              <section className="rounded-xl border bg-muted/20 p-4" aria-labelledby="evidence-source-heading">
                <h2 id="evidence-source-heading" className="text-xs font-semibold uppercase tracking-[0.12em] text-muted-foreground">Evidence source</h2>
                <p className="mt-2 text-sm font-semibold">{evidence?.subject || draft.delivery_context.context_packet_ref || "No evidence linked"}</p>
                <p className="mt-1 text-xs leading-5 text-muted-foreground">
                  Evidence supports drafting and fact-checking only. It never controls reply headers or recipients.
                </p>
                {evidence?.body_snippet && <p className="mt-3 line-clamp-3 whitespace-pre-wrap rounded-lg bg-background p-3 text-xs leading-5">{evidence.body_snippet}</p>}
              </section>
              <section className={cn("rounded-xl border p-4", draft.delivery_context.delivery_mode === "unresolved" ? "border-warning/40 bg-warning/5" : "bg-muted/20")} aria-labelledby="send-as-heading">
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div><h2 id="send-as-heading" className="text-xs font-semibold uppercase tracking-[0.12em] text-muted-foreground">Send as</h2><p className="mt-2 text-sm font-semibold">{draft.delivery_context.delivery_mode === "reply" ? `Reply: ${confirmedThread?.subject || draft.delivery_context.reply_to_email_id}` : draft.delivery_context.delivery_mode === "new" ? "New email" : "Confirmation required"}</p></div>
                  <Badge variant="outline" className={statusTone(draft.delivery_context.delivery_mode)}>{draft.delivery_context.delivery_mode}</Badge>
                </div>
                {draft.delivery_context.delivery_mode === "unresolved" && <p className="mt-2 text-xs leading-5 text-warning">A suggested thread is not a confirmed reply. Choose New email or explicitly select the correct customer message.</p>}
                {suggestedThread && <p className="mt-2 text-xs text-muted-foreground">Suggested: {suggestedThread.subject || suggestedThread.email_id}</p>}
                <div className="mt-3 flex flex-wrap gap-2">
                  <Button type="button" size="sm" variant={draft.delivery_context.delivery_mode === "new" ? "secondary" : "outline"} disabled={readOnly || Boolean(busyAction)} onClick={() => void handleConfirmNew()}>
                    <Send className="size-4" /> Confirm new email
                  </Button>
                  {suggestedThread && (
                    <Button type="button" size="sm" variant="outline" disabled={readOnly || Boolean(busyAction)} onClick={() => void handleConfirmReply(suggestedThread.email_id)}>
                      Confirm suggested reply
                    </Button>
                  )}
                </div>
              </section>
            </CardContent>
          </Card>

          <Card className="border border-border/80 shadow-sm">
            <CardHeader className="border-b">
              <CardTitle className="flex flex-wrap items-center justify-between gap-2">
                <span>Message</span>
                {readOnly && <Badge variant="outline"><LockKeyhole className="size-3" /> Read-only</Badge>}
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              {lockedByDelivery && (
                <div className="flex gap-2 rounded-lg border border-info/30 bg-info/8 p-3 text-sm"><LockKeyhole className="mt-0.5 size-4 shrink-0 text-info" /><span>{reviewState === "requested" ? "The composer is locked while review is pending." : `The composer is locked while delivery is ${draft.delivery_status.replaceAll("_", " ")}.`}</span></div>
              )}
              <div className="grid gap-1.5 sm:grid-cols-[4rem_minmax(0,1fr)] sm:items-center">
                <label htmlFor="sender-identity" className="text-xs font-semibold uppercase tracking-[0.12em] text-muted-foreground">From</label>
                <div>
                  <select
                    id="sender-identity"
                    value={form.envelope.sender_identity_id || ""}
                    disabled={readOnly}
                    aria-invalid={Boolean(issueFor("sender_identity"))}
                    aria-describedby={issueFor("sender_identity") ? "sender-identity-help" : undefined}
                    onChange={(event) => updateForm((current) => ({ ...current, envelope: { ...current.envelope, sender_identity_id: event.target.value || null } }))}
                    className={cn("min-h-11 w-full rounded-lg border bg-background px-3 text-sm outline-none focus:border-primary/60 focus:ring-3 focus:ring-primary/10", issueFor("sender_identity") && "border-danger/70 ring-3 ring-danger/10", readOnly && "bg-muted/30")}
                  >
                    <option value="">Select an approved sender identity</option>
                    {workspace.sender_identities.map((identity) => <option key={identity.identity_id} value={identity.identity_id}>{identity.display_name} &lt;{identity.email_address}&gt;</option>)}
                  </select>
                  {issueFor("sender_identity") && <p id="sender-identity-help" className="mt-1 text-xs text-danger">{issueFor("sender_identity")?.message}</p>}
                </div>
              </div>

              <RecipientField id="recipients-to" label="To" recipients={form.envelope.to} contacts={workspace.contacts} readOnly={readOnly} invalid={Boolean(issueFor("envelope.to", "recipient_to"))} helperText={issueFor("envelope.to", "recipient_to")?.message} onChange={(to) => updateForm((current) => ({ ...current, envelope: { ...current.envelope, to } }))} />
              <RecipientField id="recipients-cc" label="CC" recipients={form.envelope.cc} contacts={workspace.contacts} readOnly={readOnly} invalid={Boolean(issueFor("envelope.cc"))} helperText={issueFor("envelope.cc")?.message} onChange={(cc) => updateForm((current) => ({ ...current, envelope: { ...current.envelope, cc } }))} />
              <div>
                <button type="button" className="ml-auto flex min-h-10 items-center gap-1 rounded-lg px-2 text-xs font-semibold text-muted-foreground outline-none hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50 sm:ml-16" aria-expanded={showBcc} aria-controls="bcc-field" onClick={() => setShowBcc((value) => !value)}>
                  {showBcc ? <ChevronUp className="size-3.5" /> : <ChevronDown className="size-3.5" />} {showBcc ? "Hide BCC" : `Show BCC${form.envelope.bcc.length ? ` (${form.envelope.bcc.length})` : ""}`}
                </button>
                {showBcc && <div id="bcc-field"><RecipientField id="recipients-bcc" label="BCC" recipients={form.envelope.bcc} contacts={workspace.contacts} readOnly={readOnly} invalid={Boolean(issueFor("envelope.bcc"))} helperText={issueFor("envelope.bcc")?.message || "BCC recipients are always highlighted again in final preview."} onChange={(bcc) => updateForm((current) => ({ ...current, envelope: { ...current.envelope, bcc } }))} /></div>}
              </div>

              <div className="grid gap-1.5 sm:grid-cols-[4rem_minmax(0,1fr)] sm:items-start">
                <label htmlFor="draft-subject" className="pt-3 text-xs font-semibold uppercase tracking-[0.12em] text-muted-foreground">Subject</label>
                <div>
                  <input id="draft-subject" value={form.subject} readOnly={readOnly} aria-invalid={Boolean(issueFor("subject"))} aria-describedby={issueFor("subject") ? "draft-subject-help" : undefined} onChange={(event) => updateForm((current) => ({ ...current, subject: event.target.value }))} className={cn("min-h-11 w-full rounded-lg border bg-background px-3 text-sm font-medium outline-none focus:border-primary/60 focus:ring-3 focus:ring-primary/10", issueFor("subject") && "border-danger/70 ring-3 ring-danger/10", readOnly && "bg-muted/30")} placeholder="Email subject" />
                  {issueFor("subject") && <p id="draft-subject-help" className="mt-1 text-xs text-danger">{issueFor("subject")?.message}</p>}
                </div>
              </div>

              <div id="draft-body" tabIndex={-1}>
                <div className="mb-2 flex items-center justify-between gap-2"><label className="text-xs font-semibold uppercase tracking-[0.12em] text-muted-foreground">Body</label><span className="text-xs text-muted-foreground">Sanitized by the server on every save</span></div>
                <RichTextEditor value={form.bodyHtml} readOnly={readOnly} invalid={Boolean(issueFor("body"))} onChange={(bodyHtml) => updateForm((current) => ({ ...current, bodyHtml }))} />
                {issueFor("body") && <p className="mt-1 text-xs text-danger">{issueFor("body")?.message}</p>}
              </div>
            </CardContent>
          </Card>

          <AttachmentsPanel workspace={workspace} readOnly={readOnly} leaseToken={leaseToken} flushAutosave={flushAutosaveRef.current} onWorkspaceChange={installWorkspace} onError={setActionError} />

          <Card size="sm" id="review" className="border border-border/80 shadow-sm">
            <CardHeader className="border-b"><CardTitle className="flex items-center justify-between gap-2"><span className="flex items-center gap-2"><FileCheck2 className="size-4 text-primary" /> Review</span><Badge variant="outline" className={statusTone(reviewState)}>{reviewState.replaceAll("_", " ")}</Badge></CardTitle></CardHeader>
            <CardContent className="space-y-4">
              <div className="grid gap-3 text-sm sm:grid-cols-2"><div className="rounded-lg bg-muted/30 p-3"><p className="text-xs font-semibold uppercase text-muted-foreground">Owner</p><p className="mt-1 font-medium">{review.owner_user_id || "Unassigned"}</p></div><div className="rounded-lg bg-muted/30 p-3"><p className="text-xs font-semibold uppercase text-muted-foreground">Reviewer</p><p className="mt-1 font-medium">{review.reviewer_user_id || "Not requested"}</p></div></div>
              {reviewState === "requested" ? (
                <div className="space-y-3">
                  <p className="text-sm text-muted-foreground">Approval is bound to revision {draft.revision.number} and the full content, envelope, attachments, and delivery hash.</p>
                  <div className="flex flex-wrap gap-2">
                    {userIsReviewer && <Button type="button" disabled={Boolean(busyAction)} onClick={() => void handleReviewAction("approve")}><ShieldCheck className="size-4" /> Approve exact revision</Button>}
                    {userIsReviewer && <Button type="button" variant="outline" disabled={Boolean(busyAction)} onClick={() => void handleReviewAction("changes")}>Request changes</Button>}
                    {userCanCancelReview && <Button type="button" variant="destructive" disabled={Boolean(busyAction)} onClick={() => void handleReviewAction("cancel")}>Cancel review</Button>}
                    {!userIsReviewer && !userCanCancelReview && <p className="text-xs text-muted-foreground">Only the assigned reviewer can decide; the requester can cancel.</p>}
                  </div>
                </div>
              ) : (
                <div className="space-y-2">
                  {reviewState === "approved" && <div className="flex gap-2 rounded-lg border border-success/30 bg-success/8 p-3 text-sm"><CheckCircle2 className="mt-0.5 size-4 shrink-0 text-success" /> Approved for the current send-affecting hash. Any content, recipient, attachment, sender, or thread change makes it stale.</div>}
                  {reviewState === "stale" && <div className="flex gap-2 rounded-lg border border-danger/30 bg-danger/8 p-3 text-sm text-danger"><AlertCircle className="mt-0.5 size-4 shrink-0" /> The prior approval is stale. Request review again after saving.</div>}
                  {reviewState === "changes_requested" && <div className="flex gap-2 rounded-lg border border-warning/35 bg-warning/8 p-3 text-sm"><AlertCircle className="mt-0.5 size-4 shrink-0 text-warning" /> Changes were requested. Editing is enabled, but delivery remains blocked until review is requested and approved.</div>}
                  {reviewState !== "approved" && (
                    <div className="flex flex-col gap-2 sm:flex-row">
                      <label htmlFor="reviewer-user-id" className="sr-only">Reviewer user ID</label>
                      <input id="reviewer-user-id" value={reviewerUserId} onChange={(event) => setReviewerUserId(event.target.value)} placeholder="Reviewer user ID" disabled={readOnly} className="min-h-11 min-w-0 flex-1 rounded-lg border bg-background px-3 text-sm outline-none focus:border-primary/60 focus:ring-3 focus:ring-primary/10" />
                      <Button type="button" disabled={readOnly || Boolean(busyAction) || !reviewerUserId.trim()} onClick={() => void handleRequestReview()}><UserRoundCheck className="size-4" /> Request review</Button>
                    </div>
                  )}
                  <p className="text-xs text-muted-foreground">Review is optional until requested. Mandatory safety checks always apply.</p>
                </div>
              )}
            </CardContent>
          </Card>

          <PreflightPanel preflight={workspace.preflight} stale={preflightStale} refreshing={busyAction === "preflight"} onRefresh={() => void refreshPreflight()} onFocusField={focusField} />
        </main>

        <aside className="min-w-0 xl:sticky xl:top-5 xl:max-h-[calc(100vh-2.5rem)] xl:overflow-y-auto xl:pr-1" aria-label="Draft context and collaboration">
          <WorkspaceSidePanel workspace={workspace} project={project} memory={memory} busyAction={busyAction} readOnly={readOnly} onRefresh={refreshWorkspace} onConfirmNew={handleConfirmNew} onConfirmReply={handleConfirmReply} onRestore={handleRestore} onApplyProposal={handleApplyProposal} onRejectProposal={handleRejectProposal} onError={setActionError} />
        </aside>
      </div>

      <div className="sticky bottom-3 z-30 rounded-2xl border bg-card/95 p-2 shadow-xl backdrop-blur supports-[backdrop-filter]:bg-card/85 xl:hidden" aria-label="Draft actions">
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
          <Button type="button" variant="outline" disabled={readOnly || !dirty || saveState === "saving"} onClick={() => void handleSave()}><Save className="size-4" /> Save</Button>
          <Button type="button" variant="outline" className="hidden sm:inline-flex" disabled={readOnly || Boolean(busyAction)} onClick={() => void handleCheckpoint()}><FileCheck2 className="size-4" /> Checkpoint</Button>
          <Button type="button" disabled={readOnly || Boolean(busyAction) || retryable} onClick={() => void openFinalPreview()}><Eye className="size-4" /> Preview &amp; send</Button>
        </div>
      </div>

      <FinalPreviewDialog open={previewOpen} preflight={preview} busy={["send", "schedule", "retry"].includes(busyAction || "")} retryOnly={retryMode} onClose={() => setPreviewOpen(false)} onSendNow={handleSendNow} onSendLater={handleSendLater} />

      <WorkspaceDialog
        open={Boolean(conflict)}
        title="Revision conflict"
        description="Another saved revision exists. This workspace will never overwrite it silently."
        onClose={() => undefined}
        footer={(
          <>
            <Button type="button" variant="outline" disabled={Boolean(busyAction)} onClick={() => void reloadConflict()}><RefreshCw className="size-4" /> Load latest</Button>
            <Button type="button" disabled={Boolean(busyAction) || !leaseToken} onClick={() => void reapplyConflict()}><RotateCcw className="size-4" /> Reapply my version</Button>
          </>
        )}
      >
        {conflict && (
          <div className="space-y-4">
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="rounded-xl border p-4"><p className="text-xs font-semibold uppercase text-muted-foreground">Your unsaved subject</p><p className="mt-2 break-words text-sm">{form.subject || "(No subject)"}</p></div>
              <div className="rounded-xl border border-info/30 bg-info/5 p-4"><p className="text-xs font-semibold uppercase text-muted-foreground">Latest server subject - revision {conflict.revision.number}</p><p className="mt-2 break-words text-sm">{conflict.content.subject || "(No subject)"}</p></div>
            </div>
            <p className="text-sm text-muted-foreground">Load latest discards this local working form. Reapply explicitly writes your complete local form on top of the latest revision and creates a normal audited revision.</p>
          </div>
        )}
      </WorkspaceDialog>
    </div>
  );
}
