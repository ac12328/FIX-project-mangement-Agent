"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { ArrowLeft, Reply, Save, Send, Trash2 } from "lucide-react";

import { DraftAttachmentsCard } from "@/components/draft-attachments-card";
import { StatusBadge } from "@/components/status-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import {
  approveAndSendDraft,
  draftAction,
  getDraft,
  getMemory,
  getProject,
  updateDraft,
} from "@/lib/api";
import type { Draft, MemoryItem, ProjectDetail } from "@/lib/types";
import { cn } from "@/lib/utils";

export function LegacyDraftDetailView({ draftId }: { draftId: string }) {
  const id = draftId;
  const router = useRouter();
  const [draft, setDraft] = useState<Draft | null>(null);
  const [project, setProject] = useState<ProjectDetail | null>(null);
  const [memory, setMemory] = useState<MemoryItem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [editText, setEditText] = useState("");
  const [toEmail, setToEmail] = useState("");
  const [ccEmail, setCcEmail] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveMsg, setSaveMsg] = useState<string | null>(null);
  const [acting, setActing] = useState<string | null>(null);
  const [sendMsg, setSendMsg] = useState<string | null>(null);

  useEffect(() => {
    if (!id) return;
    getDraft(id)
      .then((d) => {
        setDraft(d);
        setEditText(d.current_draft_text ?? "");
        setToEmail(d.to_email ?? "");
        // Auto-populate CC from the original email thread when draft CC is empty
        setCcEmail(d.cc || d.thread_cc || "");
        if (d.project_id) {
          getProject(d.project_id).then(setProject).catch(() => null);
          getMemory(d.project_id).then(setMemory).catch(() => null);
        }
      })
      .catch((e) => setError(e.message));
  }, [id]);

  const saveEdit = async () => {
    if (!draft) return;
    setSaving(true);
    setSaveMsg(null);
    try {
      const updated = await updateDraft(draft.draft_id, {
        body: editText,
        to_email: toEmail,
        cc: ccEmail,
      });
      setDraft(updated);
      setSaveMsg("Saved.");
    } catch (e: unknown) {
      setSaveMsg(e instanceof Error ? e.message : "Save failed");
    } finally {
      setSaving(false);
    }
  };

  const act = async (action: "approve" | "discard") => {
    if (!draft) return;
    setActing(action);
    setError(null);
    try {
      if (action === "approve" && draft.status !== "approved") {
        // Always persist latest edits before sending a reviewable draft.
        await updateDraft(draft.draft_id, {
          body: editText,
          to_email: toEmail,
          cc: ccEmail,
        });
      }
      const result = action === "approve"
        ? await approveAndSendDraft(draft.draft_id)
        : await draftAction(draft.draft_id, action);
      if (!result.success) {
        throw new Error(result.message);
      }
      if (action === "approve") {
        const scheduled = result.message.toLowerCase().includes("scheduled");
        setDraft((d) => d ? { ...d, status: scheduled ? "approved" : "sent" } : d);
        setSendMsg(scheduled ? result.message : "Email sent successfully! Returning to drafts...");
        if (!scheduled) {
          setTimeout(() => router.push("/mail?tab=drafts"), 2000);
        }
      } else {
        setDraft((d) => d ? { ...d, status: "discarded" } : d);
        router.push("/mail?tab=drafts");
      }
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Action failed");
    } finally {
      setActing(null);
    }
  };

  if (error)
    return (
      <p className="py-12 text-center text-sm text-danger">Error: {error}</p>
    );

  if (!draft)
    return (
      <div className="space-y-4 p-4">
        <Skeleton className="h-6 w-64" />
        <Skeleton className="h-64 w-full" />
      </div>
    );

  const terminal = draft.status === "sent" || draft.status === "discarded";
  const approvedUnsent = draft.status === "approved";
  const readOnly = terminal || approvedUnsent;

  const wordCount = editText.trim() ? editText.trim().split(/\s+/).length : 0;

  return (
    <div className="mx-auto w-full max-w-[1600px] space-y-5">
      {/* Header */}
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start">
        <Link
          href={draft.ticket_id ? `/support/${draft.ticket_id}` : "/mail?tab=drafts"}
          className="self-start"
        >
          <Button size="sm" variant="ghost" className="gap-1.5">
            <ArrowLeft className="size-4" />
            {draft.ticket_id ? "Back to Ticket" : "Back"}
          </Button>
        </Link>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h1 className="min-w-0 text-xl font-semibold leading-tight sm:text-2xl">
              {draft.subject ?? draft.draft_id}
            </h1>
            <StatusBadge value={draft.status} />
            <Badge variant="outline" className="text-xs">
              v{draft.draft_version}
            </Badge>
          </div>
          <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
            <Link
              href={`/projects/${draft.project_id}`}
              className="font-mono font-medium text-primary hover:underline"
            >
              {draft.project_id}
            </Link>
            {draft.ticket_id && (
              <Link
                href={`/support/${draft.ticket_id}`}
                className="font-mono font-medium text-primary hover:underline"
              >
                {draft.ticket_id}
              </Link>
            )}
          </div>
        </div>
      </div>

      {sendMsg && (
        <div
          className="rounded-xl border border-success/40 bg-success/10 px-4 py-3 text-sm font-medium text-success"
          aria-live="polite"
        >
          {sendMsg}
        </div>
      )}

      {draft.thread_subject && (
        <div className="flex items-start gap-3 rounded-xl border border-primary/20 bg-primary/5 px-4 py-3 text-sm">
          <Reply className="mt-0.5 size-4 shrink-0 text-primary" />
          <div className="min-w-0 leading-6">
            <span className="font-medium text-foreground">Replying in thread: </span>
            <span className="text-muted-foreground">{draft.thread_subject}</span>
            {draft.thread_sender && (
              <span className="text-muted-foreground"> from {draft.thread_sender}</span>
            )}
          </div>
        </div>
      )}

      <div className="grid items-start gap-5 xl:grid-cols-[minmax(0,1fr)_18rem] 2xl:grid-cols-[minmax(0,1fr)_20rem]">
        {/* Primary email workspace */}
        <Card className="min-w-0 border border-border/80 bg-card shadow-sm">
          <CardHeader className="border-b border-border/70">
            <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
              <div>
                <CardTitle>Email draft</CardTitle>
                <p className="mt-1 text-xs leading-5 text-muted-foreground">
                  Review the recipients and message before approving delivery.
                </p>
              </div>
              <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                <Badge variant="outline" className="font-normal">
                  {readOnly ? "Read only" : "Editing"}
                </Badge>
                <span>{wordCount.toLocaleString()} words</span>
                <span aria-hidden="true">/</span>
                <span>{editText.length.toLocaleString()} characters</span>
              </div>
            </div>
          </CardHeader>

          <CardContent className="space-y-5">
            <section aria-labelledby="email-envelope-heading">
              <h2
                id="email-envelope-heading"
                className="mb-2 text-xs font-medium uppercase tracking-[0.14em] text-muted-foreground"
              >
                Recipients &amp; subject
              </h2>
              <div className="overflow-hidden rounded-xl border border-border/80 bg-background">
                <div className="grid gap-2 border-b border-border/70 px-3 py-3 sm:grid-cols-[5rem_minmax(0,1fr)] sm:items-center sm:px-4">
                  <label htmlFor="draft-to" className="text-xs font-medium text-muted-foreground">
                    To
                  </label>
                  {readOnly ? (
                    <span className="break-all font-mono text-sm">{draft.to_email ?? "Not set"}</span>
                  ) : (
                    <input
                      id="draft-to"
                      type="email"
                      value={toEmail}
                      onChange={(e) => setToEmail(e.target.value)}
                      placeholder="recipient@example.com"
                      className="h-10 min-w-0 rounded-lg border border-border bg-background px-3 font-mono text-sm outline-none placeholder:text-muted-foreground/50 focus:border-primary/60 focus:ring-3 focus:ring-primary/15"
                    />
                  )}
                </div>
                <div className="grid gap-2 border-b border-border/70 px-3 py-3 sm:grid-cols-[5rem_minmax(0,1fr)] sm:items-center sm:px-4">
                  <label htmlFor="draft-cc" className="text-xs font-medium text-muted-foreground">
                    CC
                  </label>
                  {readOnly ? (
                    <span className="break-all font-mono text-sm">{draft.cc ?? "Not set"}</span>
                  ) : (
                    <input
                      id="draft-cc"
                      type="text"
                      value={ccEmail}
                      onChange={(e) => setCcEmail(e.target.value)}
                      placeholder="Add CC recipients"
                      className="h-10 min-w-0 rounded-lg border border-border bg-background px-3 font-mono text-sm outline-none placeholder:text-muted-foreground/50 focus:border-primary/60 focus:ring-3 focus:ring-primary/15"
                    />
                  )}
                </div>
                <div className="grid gap-2 px-3 py-3 sm:grid-cols-[5rem_minmax(0,1fr)] sm:items-center sm:px-4">
                  <span className="text-xs font-medium text-muted-foreground">Subject</span>
                  <span className="break-words text-sm font-medium">{draft.subject ?? "Not set"}</span>
                </div>
              </div>
            </section>

            {!toEmail && !readOnly && (
              <div className="rounded-xl border border-danger/40 bg-danger/5 px-4 py-3 text-sm text-danger">
                Recipient missing — add a To address before sending.
              </div>
            )}

            <section aria-labelledby="email-message-heading">
              <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                <h2
                  id="email-message-heading"
                  className="text-xs font-medium uppercase tracking-[0.14em] text-muted-foreground"
                >
                  Message
                </h2>
                {!readOnly && (
                  <span className="text-xs text-muted-foreground">Drag the lower edge to resize</span>
                )}
              </div>
              <textarea
                aria-label="Email message"
                value={editText}
                onChange={(e) => setEditText(e.target.value)}
                disabled={readOnly}
                spellCheck
                className={cn(
                  "min-h-[32rem] w-full resize-y rounded-xl border border-border bg-background px-4 py-4 font-sans text-[15px] leading-7 shadow-inner sm:min-h-[38rem] sm:px-5 xl:min-h-[65vh]",
                  "outline-none focus:border-primary/60 focus:ring-3 focus:ring-primary/15",
                  "disabled:cursor-default disabled:bg-muted/20 disabled:text-foreground disabled:opacity-100",
                )}
              />
            </section>

            <div className="flex flex-col gap-3 border-t border-border/70 pt-4 sm:flex-row sm:items-center sm:justify-between">
              <div className="min-h-5 text-xs text-muted-foreground" aria-live="polite">
                {saveMsg ? (
                  <span className={saveMsg === "Saved." ? "text-success" : "text-danger"}>
                    {saveMsg}
                  </span>
                ) : readOnly ? (
                  "This draft can no longer be edited."
                ) : (
                  "Save your latest edits before leaving this page."
                )}
              </div>
              {!terminal && (
                <div className="flex flex-wrap items-center gap-2 sm:justify-end">
                  {!approvedUnsent && (
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={saving}
                      onClick={saveEdit}
                    >
                      <Save className="size-4" />
                      {saving ? "Saving..." : "Save Edits"}
                    </Button>
                  )}
                  <Button
                    size="sm"
                    disabled={acting === "approve" || !toEmail.trim()}
                    onClick={() => act("approve")}
                  >
                    <Send className="size-4" />
                    {acting === "approve"
                      ? "Sending..."
                      : approvedUnsent
                        ? "Retry Send"
                        : "Approve & Send"}
                  </Button>
                  <Button
                    size="sm"
                    variant="destructive"
                    disabled={acting === "discard"}
                    onClick={() => act("discard")}
                  >
                    <Trash2 className="size-4" />
                    Discard
                  </Button>
                </div>
              )}
            </div>
          </CardContent>
        </Card>

        {/* Secondary reference panel */}
        <aside aria-label="Draft references" className="space-y-4 xl:sticky xl:top-20">
          <DraftAttachmentsCard draftId={id} readOnly={readOnly} />

          {project && (
            <Card size="sm" className="border border-border/80 bg-card shadow-sm">
              <CardHeader className="border-b border-border/70">
                <CardTitle>Project context</CardTitle>
              </CardHeader>
              <CardContent className="space-y-3">
                <CtxRow label="Client" value={project.client_name} />
                <CtxRow label="Status" value={project.project_status} />
                <CtxRow label="Ball with" value={project.ball_with} />
                <CtxRow label="Milestone" value={project.current_milestone ?? "Not set"} />
                {project.next_action && (
                  <CtxRow label="Next action" value={project.next_action} />
                )}
              </CardContent>
            </Card>
          )}

          {memory.length > 0 && (
            <Card size="sm" className="border border-border/80 bg-card shadow-sm">
              <CardHeader className="border-b border-border/70">
                <CardTitle>Working memory</CardTitle>
              </CardHeader>
              <CardContent className="space-y-3">
                {memory.slice(0, 8).map((m) => (
                  <CtxRow
                    key={m.memory_key}
                    label={m.memory_key}
                    value={
                      m.memory_value == null
                        ? ""
                        : m.memory_value.length > 120
                          ? m.memory_value.slice(0, 120) + "..."
                          : m.memory_value
                    }
                  />
                ))}
              </CardContent>
            </Card>
          )}
        </aside>
      </div>
    </div>
  );
}

function CtxRow({ label, value }: { label: string; value: string | null }) {
  return (
    <div className="grid gap-1 border-b border-border/60 pb-3 last:border-b-0 last:pb-0">
      <span className="text-xs font-medium uppercase tracking-[0.12em] text-muted-foreground">
        {label}
      </span>
      <span className="break-words text-xs leading-5">{value ?? "Not set"}</span>
    </div>
  );
}
