"use client";

import { useMemo, useState } from "react";
import {
  Check,
  CheckCircle2,
  FileClock,
  Lightbulb,
  Mail,
  MessageSquare,
  Reply,
  ShieldQuestion,
  X,
} from "lucide-react";

import { formatDate } from "@/components/draft-workspace/draft-workspace-shared";
import { WorkspaceSidePanelHistory } from "@/components/draft-workspace/workspace-side-panel-history";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import {
  type AIProposal,
  type DeliveryCandidate,
  type DraftWorkspace,
  type WorkspaceComment,
  createDraftComment,
  setDraftCommentResolved,
} from "@/lib/draft-workspace-api";
import type { MemoryItem, ProjectDetail } from "@/lib/types";
import { cn } from "@/lib/utils";

interface WorkspaceSidePanelProps {
  workspace: DraftWorkspace;
  project: ProjectDetail | null;
  memory: MemoryItem[];
  busyAction: string | null;
  readOnly: boolean;
  onRefresh: () => Promise<void>;
  onConfirmNew: () => Promise<void>;
  onConfirmReply: (emailId: string) => Promise<void>;
  onRestore: (version: number) => Promise<void>;
  onApplyProposal: (proposal: AIProposal, selectedChanges?: string[]) => Promise<void>;
  onRejectProposal: (proposal: AIProposal) => Promise<void>;
  onError: (message: string) => void;
}

function CandidateCard({
  candidate,
  disabled,
  onConfirm,
}: {
  candidate: DeliveryCandidate;
  disabled: boolean;
  onConfirm: () => void;
}) {
  return (
    <article
      className={cn(
        "rounded-xl border p-3",
        candidate.confirmed && "border-primary/40 bg-primary/5",
        candidate.suggested && !candidate.confirmed && "border-info/35 bg-info/5",
      )}
    >
      <div className="flex items-start gap-2">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <h4 className="truncate text-sm font-semibold">
              {candidate.subject || "(No subject)"}
            </h4>
            {candidate.confirmed && (
              <Badge variant="outline" className="border-primary/30 bg-primary/10 text-primary">
                <Check className="size-3" /> Reply anchor
              </Badge>
            )}
            {candidate.suggested && !candidate.confirmed && (
              <Badge variant="outline" className="border-info/30 bg-info/10 text-info">
                <Lightbulb className="size-3" /> Suggested
              </Badge>
            )}
            {candidate.is_evidence_source && (
              <Badge variant="outline">
                <ShieldQuestion className="size-3" /> Evidence
              </Badge>
            )}
          </div>
          <p className="mt-1 text-xs text-muted-foreground">
            {candidate.direction === "outbound"
              ? "Sent by us"
              : `From ${candidate.sender_email || "unknown sender"}`} ·{" "}
            {formatDate(candidate.received_at || candidate.processed_at)}
          </p>
        </div>
        {!candidate.confirmed && (
          <Button type="button" size="xs" variant="outline" disabled={disabled} onClick={onConfirm}>
            <Reply className="size-3.5" /> Reply here
          </Button>
        )}
      </div>
      {candidate.body_snippet && (
        <p className="mt-3 line-clamp-4 whitespace-pre-wrap text-xs leading-5 text-muted-foreground">
          {candidate.body_snippet}
        </p>
      )}
    </article>
  );
}

function ContextRow({ label, value }: { label: string; value: string | null | undefined }) {
  return (
    <div className="grid gap-1 border-b pb-3 last:border-0 last:pb-0">
      <dt className="text-xs font-semibold uppercase tracking-[0.12em] text-muted-foreground">
        {label}
      </dt>
      <dd className="break-words text-xs leading-5">{value || "Not set"}</dd>
    </div>
  );
}

function CommentItem({
  comment,
  replies,
  busy,
  updating,
  onReply,
  onResolve,
}: {
  comment: WorkspaceComment;
  replies: WorkspaceComment[];
  busy: boolean;
  updating: boolean;
  onReply: (comment: WorkspaceComment) => void;
  onResolve: (comment: WorkspaceComment) => void;
}) {
  return (
    <article className={cn("rounded-xl border p-3", comment.resolved_at && "bg-muted/30 opacity-75")}>
      <header className="flex items-start justify-between gap-2">
        <div>
          <p className="text-xs font-semibold">{comment.author_display_name}</p>
          <p className="text-xs text-muted-foreground">
            {comment.location_type} · {formatDate(comment.created_at)}
          </p>
        </div>
        {comment.resolved_at && (
          <Badge variant="outline">
            <CheckCircle2 className="size-3" /> Resolved
          </Badge>
        )}
      </header>
      <p className="mt-2 whitespace-pre-wrap text-sm leading-5">{comment.body}</p>
      <div className="mt-2 flex gap-1">
        <Button type="button" size="xs" variant="ghost" disabled={busy} onClick={() => onReply(comment)}>
          <Reply className="size-3" /> Reply
        </Button>
        <Button type="button" size="xs" variant="ghost" disabled={busy} onClick={() => onResolve(comment)}>
          {updating ? "Updating…" : comment.resolved_at ? "Reopen" : "Resolve"}
        </Button>
      </div>
      {replies.length > 0 && (
        <div className="mt-3 space-y-2 border-l-2 pl-3">
          {replies.map((reply) => (
            <div key={reply.comment_id} className="rounded-lg bg-muted/35 p-2.5">
              <p className="text-xs font-semibold">
                {reply.author_display_name}{" "}
                <span className="font-normal text-muted-foreground">· {formatDate(reply.created_at)}</span>
              </p>
              <p className="mt-1 whitespace-pre-wrap text-xs leading-5">{reply.body}</p>
            </div>
          ))}
        </div>
      )}
    </article>
  );
}


export function WorkspaceSidePanel({
  workspace,
  project,
  memory,
  busyAction,
  readOnly,
  onRefresh,
  onConfirmNew,
  onConfirmReply,
  onRestore,
  onApplyProposal,
  onRejectProposal,
  onError,
}: WorkspaceSidePanelProps) {
  const [commentBody, setCommentBody] = useState("");
  const [commentLocation, setCommentLocation] =
    useState<WorkspaceComment["location_type"]>("general");
  const [replyTo, setReplyTo] = useState<WorkspaceComment | null>(null);
  const [commentAction, setCommentAction] = useState<string | null>(null);

  const rootComments = workspace.comments.filter((comment) => !comment.parent_comment_id);
  const repliesByParent = useMemo(() => {
    const result = new Map<string, WorkspaceComment[]>();
    for (const comment of workspace.comments) {
      if (!comment.parent_comment_id) continue;
      result.set(comment.parent_comment_id, [...(result.get(comment.parent_comment_id) ?? []), comment]);
    }
    return result;
  }, [workspace.comments]);

  const evidence = workspace.delivery_candidates.filter((candidate) => candidate.is_evidence_source);
  const mutationBusy = Boolean(busyAction) || commentAction !== null;

  const submitComment = async () => {
    const body = commentBody.trim();
    if (!body || mutationBusy) return;

    setCommentAction("submit");
    try {
      await createDraftComment(workspace.draft.draft_id, {
        body,
        location_type: replyTo ? replyTo.location_type : commentLocation,
        location_ref: replyTo?.location_ref ?? null,
        parent_comment_id: replyTo?.comment_id ?? null,
      });
      setCommentBody("");
      setReplyTo(null);
      await onRefresh();
    } catch (error) {
      onError(error instanceof Error ? error.message : "Could not create comment.");
    } finally {
      setCommentAction(null);
    }
  };

  const resolveComment = async (comment: WorkspaceComment) => {
    if (mutationBusy) return;

    setCommentAction(comment.comment_id);
    try {
      await setDraftCommentResolved(
        workspace.draft.draft_id,
        comment.comment_id,
        !comment.resolved_at,
      );
      await onRefresh();
    } catch (error) {
      onError(error instanceof Error ? error.message : "Could not update comment.");
    } finally {
      setCommentAction(null);
    }
  };

  return (
    <div className="rounded-xl border bg-card shadow-sm xl:sticky xl:top-20">
      <Tabs defaultValue="thread" className="gap-0">
        <div className="overflow-x-auto border-b px-2 pt-2">
          <TabsList variant="line" className="min-w-max">
            <TabsTrigger value="thread"><Mail className="size-3.5" /> Thread</TabsTrigger>
            <TabsTrigger value="evidence"><ShieldQuestion className="size-3.5" /> Evidence</TabsTrigger>
            <TabsTrigger value="facts"><Lightbulb className="size-3.5" /> Facts</TabsTrigger>
            <TabsTrigger value="comments">
              <MessageSquare className="size-3.5" /> Comments
              {workspace.comments.length ? ` (${workspace.comments.length})` : ""}
            </TabsTrigger>
            <TabsTrigger value="history"><FileClock className="size-3.5" /> Versions</TabsTrigger>
          </TabsList>
        </div>

        <TabsContent value="thread" className="max-h-[calc(100vh-10rem)] overflow-y-auto p-3">
          <div className="mb-3 rounded-xl border bg-muted/25 p-3">
            <p className="text-xs font-semibold uppercase tracking-[0.12em] text-muted-foreground">
              Send as
            </p>
            <div className="mt-2 flex items-start gap-2">
              <div className="min-w-0 flex-1">
                <p className="text-sm font-medium">
                  {workspace.draft.delivery_context.delivery_mode === "unresolved"
                    ? "Choose a delivery context"
                    : workspace.draft.delivery_context.delivery_mode === "new"
                      ? "New email"
                      : "Reply in customer thread"}
                </p>
                <p className="mt-1 text-xs leading-5 text-muted-foreground">
                  Evidence never controls recipients or reply headers.
                </p>
              </div>
              {workspace.draft.delivery_context.delivery_mode !== "new" && (
                <Button
                  type="button"
                  size="xs"
                  variant="outline"
                  disabled={readOnly || mutationBusy}
                  onClick={() => void onConfirmNew()}
                >
                  New email
                </Button>
              )}
            </div>
          </div>
          <div className="space-y-2">
            {workspace.delivery_candidates.length ? (
              workspace.delivery_candidates.map((candidate) => (
                <CandidateCard
                  key={candidate.email_id}
                  candidate={candidate}
                  disabled={readOnly || mutationBusy}
                  onConfirm={() => void onConfirmReply(candidate.email_id)}
                />
              ))
            ) : (
              <p className="rounded-xl border border-dashed p-6 text-center text-sm text-muted-foreground">
                No customer email candidates are available. Confirm New email to continue.
              </p>
            )}
          </div>
        </TabsContent>

        <TabsContent value="evidence" className="max-h-[calc(100vh-10rem)] overflow-y-auto p-3">
          <div className="mb-3 rounded-xl border border-info/30 bg-info/5 p-3 text-xs leading-5">
            Evidence supports drafting and fact-checking only. It cannot confirm a reply thread or change the envelope.
          </div>
          {evidence.length ? (
            <div className="space-y-2">
              {evidence.map((candidate) => (
                <article key={candidate.email_id} className="rounded-xl border p-3">
                  <h4 className="text-sm font-semibold">{candidate.subject || "Evidence email"}</h4>
                  <p className="mt-1 text-xs text-muted-foreground">
                    {candidate.sender_email || "Unknown sender"} ·{" "}
                    {formatDate(candidate.received_at || candidate.processed_at)}
                  </p>
                  <p className="mt-3 whitespace-pre-wrap text-xs leading-5">
                    {candidate.body_snippet || "No evidence preview available."}
                  </p>
                </article>
              ))}
            </div>
          ) : (
            <p className="rounded-xl border border-dashed p-6 text-center text-sm text-muted-foreground">
              No evidence source is linked.
            </p>
          )}
        </TabsContent>

        <TabsContent value="facts" className="max-h-[calc(100vh-10rem)] overflow-y-auto p-3">
          {project ? (
            <dl className="space-y-3 rounded-xl border p-3">
              <ContextRow label="Client" value={project.client_name} />
              <ContextRow label="Project" value={project.project_id} />
              <ContextRow label="Status" value={project.project_status} />
              <ContextRow
                label="Milestone"
                value={project.current_milestone_display || project.current_milestone}
              />
              <ContextRow label="Ball with" value={project.ball_with} />
              <ContextRow label="Next action" value={project.next_action} />
            </dl>
          ) : (
            <p className="rounded-xl border border-dashed p-6 text-center text-sm text-muted-foreground">
              This draft is not linked to a project.
            </p>
          )}
          {memory.length > 0 && (
            <section className="mt-3 space-y-2">
              <h3 className="text-xs font-semibold uppercase tracking-[0.12em] text-muted-foreground">
                Project memory
              </h3>
              {memory.slice(0, 12).map((item) => (
                <div key={item.memory_key} className="rounded-lg border p-3">
                  <p className="text-xs font-semibold">{item.memory_key}</p>
                  <p className="mt-1 whitespace-pre-wrap text-xs leading-5 text-muted-foreground">
                    {item.memory_value}
                  </p>
                </div>
              ))}
            </section>
          )}
        </TabsContent>

        <TabsContent
          value="comments"
          className="max-h-[calc(100vh-10rem)] overflow-y-auto p-3"
          aria-busy={commentAction !== null}
        >
          <div className="mb-3 rounded-xl border p-3">
            {replyTo && (
              <div className="mb-2 flex items-center justify-between rounded-lg bg-muted/50 px-2.5 py-2 text-xs">
                <span>Replying to {replyTo.author_display_name}</span>
                <Button
                  type="button"
                  size="icon-xs"
                  variant="ghost"
                  aria-label="Cancel reply"
                  disabled={commentAction === "submit"}
                  onClick={() => setReplyTo(null)}
                >
                  <X className="size-3" />
                </Button>
              </div>
            )}
            {!replyTo && (
              <select
                value={commentLocation}
                disabled={commentAction === "submit"}
                onChange={(event) =>
                  setCommentLocation(event.target.value as WorkspaceComment["location_type"])
                }
                className="mb-2 h-9 w-full rounded-lg border bg-background px-2 text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                aria-label="Comment location"
              >
                <option value="general">General comment</option>
                <option value="subject">Subject</option>
                <option value="recipient">Recipients</option>
                <option value="body">Body</option>
                <option value="attachment">Attachment</option>
              </select>
            )}
            <Textarea
              value={commentBody}
              disabled={commentAction === "submit"}
              onChange={(event) => setCommentBody(event.target.value)}
              placeholder={replyTo ? "Write a reply…" : "Leave a review comment…"}
              aria-label={replyTo ? "Comment reply" : "Review comment"}
              className="min-h-24"
            />
            <div className="mt-2 flex justify-end">
              <Button
                type="button"
                size="sm"
                disabled={!commentBody.trim() || mutationBusy}
                onClick={() => void submitComment()}
              >
                <MessageSquare className="size-4" />
                {commentAction === "submit" ? "Posting…" : replyTo ? "Reply" : "Comment"}
              </Button>
            </div>
          </div>
          <div className="space-y-2">
            {rootComments.length ? (
              rootComments.map((comment) => (
                <CommentItem
                  key={comment.comment_id}
                  comment={comment}
                  replies={repliesByParent.get(comment.comment_id) ?? []}
                  busy={mutationBusy}
                  updating={commentAction === comment.comment_id}
                  onReply={setReplyTo}
                  onResolve={(item) => void resolveComment(item)}
                />
              ))
            ) : (
              <p className="py-6 text-center text-sm text-muted-foreground">No comments yet.</p>
            )}
          </div>
        </TabsContent>

        <WorkspaceSidePanelHistory
          workspace={workspace}
          busy={mutationBusy}
          readOnly={readOnly}
          onRestore={onRestore}
          onApplyProposal={onApplyProposal}
          onRejectProposal={onRejectProposal}
          onError={onError}
        />
      </Tabs>
    </div>
  );
}
