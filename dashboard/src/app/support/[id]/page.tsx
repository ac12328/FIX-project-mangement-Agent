"use client";

import { useCallback, useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { PageHeader } from "@/components/page-header";
import { ErrorPanel, LoadingPanel } from "@/components/state-panel";
import { StatusBadge } from "@/components/status-badge";
import { cn } from "@/lib/utils";
import { notifyError, notifySuccess } from "@/lib/notifications";
import { ArrowLeft, RefreshCw, Wifi, WifiOff } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";
import { cleanDisplayTitle, deskBriefingFromDetail, partyFromEmail } from "@/lib/support-display";
import { ErrorState } from "@/components/error-state";
import {
  getTicketDetail,
  getSupportSummary,
  postSupportAction,
  createTicketDraft,
  addTicketNote,
  TicketDetail,
  TicketAISummary,
} from "@/lib/support-api";
import { fetchJson } from "@/lib/api";
import AgentChatDrawer from "@/components/support/agent-chat-drawer";
import StatusStrip from "@/components/support/status-strip";
import ConversationThread from "@/components/support/conversation-thread";
import ContextPanel from "@/components/support/context-panel";
import ActionButtons from "@/components/support/action-buttons";
import ActivityTimeline from "@/components/support/activity-timeline";
import ProjectLinkBar from "@/components/support/project-link-bar";

export default function SupportTicketDetailPage() {
  const params = useParams();
  const router = useRouter();
  const ticketId = params.id as string;

  const [detail, setDetail] = useState<TicketDetail | null>(null);
  const [aiSummary, setAiSummary] = useState<TicketAISummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [draftText, setDraftText] = useState("");
  const [actionLoading, setActionLoading] = useState(false);
  const [creatingDraft, setCreatingDraft] = useState(false);
  const [summaryLoading, setSummaryLoading] = useState(true);
  const [summaryError, setSummaryError] = useState("");
  const [sseConnected, setSseConnected] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const d = await getTicketDetail(ticketId);
      setDetail(d);
      setDraftText(d.reply_draft_text ?? "");
      setAiSummary((current) => current ?? deskBriefingFromDetail(d));
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to load ticket");
    } finally {
      setLoading(false);
    }
  }, [ticketId]);

  const [noteOpen, setNoteOpen] = useState(false);
  const [noteText, setNoteText] = useState("");

  const handleAction = useCallback(
    async (actionType: string, actionParams: Record<string, string> = {}) => {
      setActionLoading(true);
      setError("");
      try {
        const result = await postSupportAction(ticketId, actionType, actionParams);
        if (actionType === "send_followup" && typeof result.draft_id === "string") {
          notifySuccess("Follow-up draft created");
          router.push(`/drafts/${result.draft_id}`);
          return;
        }
        notifySuccess("Ticket updated");
        await load();
      } catch (cause: unknown) {
        const message = cause instanceof Error ? cause.message : "Ticket action failed.";
        setError(message);
        notifyError(cause, message);
      } finally {
        setActionLoading(false);
      }
    },
    [ticketId, load, router],
  );

  const handleAddNote = useCallback(async () => {
    const text = noteText.trim();
    if (!text) return;
    setActionLoading(true);
    try {
      await addTicketNote(ticketId, text);
      notifySuccess("Internal note added");
      setNoteText("");
      setNoteOpen(false);
      await load();
    } catch (cause: unknown) {
      const message = cause instanceof Error ? cause.message : "Failed to add note.";
      setError(message);
      notifyError(cause, message);
    } finally {
      setActionLoading(false);
    }
  }, [ticketId, load, noteText]);

  const loadSummary = useCallback(async (force = false) => {
    setSummaryLoading(true);
    setSummaryError("");
    try {
      const summary = force
        ? await fetchJson<TicketAISummary>(`/api/support/${ticketId}/summary?force=true`, { timeoutMs: 60_000 })
        : await getSupportSummary(ticketId);
      setAiSummary(summary);
      if (summary?.reply_draft_text) setDraftText((current) => current || summary.reply_draft_text || "");
      return true;
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : "Could not load the AI summary.";
      setSummaryError(message);
      if (force) notifyError(cause, message);
      return false;
    } finally {
      setSummaryLoading(false);
    }
  }, [ticketId]);

  const handleRegenerate = useCallback(async () => {
    if (await loadSummary(true)) notifySuccess("AI summary refreshed");
  }, [loadSummary]);

  const handleReviewSend = useCallback(async () => {
    setCreatingDraft(true);
    try {
      const { draft_id } = await createTicketDraft(ticketId, draftText || undefined);
      router.push(`/drafts/${draft_id}`);
    } catch (cause: unknown) {
      notifyError(cause, "Failed to create the support draft.");
    } finally {
      setCreatingDraft(false);
    }
  }, [ticketId, draftText, router]);

  useEffect(() => { load(); }, [load]);

  useEffect(() => { void loadSummary(); }, [loadSummary]);

  // SSE subscription
  useEffect(() => {
    const sseUrl = `${process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8317"}/api/events`;
    const es = new EventSource(sseUrl);
    es.onopen = () => setSseConnected(true);
    es.onerror = () => setSseConnected(false);
    es.onmessage = (e) => {
      try {
        const evt = JSON.parse(e.data);
        if (evt.event_type === "ticket_updated" && evt.detail?.ticket_id === ticketId) {
          load();
        }
      } catch {}
    };
    return () => es.close();
  }, [ticketId, load]);

  if (loading) return <LoadingPanel label="Loading support ticket workspace" />;

  if (error) return <ErrorPanel description={error} onRetry={() => void load()} />;
  if (!detail) return <ErrorState description="Ticket not found" />;

  return (
    <div className="mx-auto max-w-[1700px] space-y-6">
      <PageHeader
        eyebrow="Support"
        title={cleanDisplayTitle(detail.title)}
        description="Who this is, what last happened, and the next action. Reply or close from here."
        metadata={
          <>
            <span className="font-mono font-semibold text-primary">{detail.ticket_id}</span>
            {detail.status ? <StatusBadge value={detail.status} /> : null}
            {detail.severity ? <StatusBadge value={detail.severity} /> : null}
            {detail.category ? <Badge variant="outline">{detail.category}</Badge> : null}
            {detail.created_at ? <span>Opened {new Date(detail.created_at).toLocaleDateString()}</span> : null}
            <span className={cn("inline-flex items-center gap-1", sseConnected ? "text-success" : "text-muted-foreground")}>
              {sseConnected ? <Wifi className="size-3.5" /> : <WifiOff className="size-3.5" />}
              {sseConnected ? "Live updates connected" : "Live updates offline"}
            </span>
          </>
        }
        actions={
          <>
            <Link href="/support" className={buttonVariants({ variant: "outline" })}>
              <ArrowLeft className="size-4" />Back to support
            </Link>
            <Button variant="outline" onClick={() => void load()} disabled={loading || actionLoading}>
              <RefreshCw className="size-4" />Refresh
            </Button>
          </>
        }
      />

      {/* 1. Status Strip */}
      <StatusStrip ball={detail.ball} status={detail.status} severity={detail.severity} followup={detail.followup} />

      <section className="grid gap-3 rounded-xl border bg-card p-4 shadow-sm sm:grid-cols-3" aria-label="Ticket identity">
        <div>
          <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Client</p>
          <p className="mt-1 text-sm font-medium">{detail.counterparty_org_name || detail.counterparty || partyFromEmail(detail.reporter_email) || "Unidentified"}</p>
          {detail.reporter_email ? <p className="mt-1 text-xs text-muted-foreground">{detail.reporter_email}</p> : null}
        </div>
        <div>
          <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Project</p>
          {detail.project.has_project ? (
            <Link href={`/projects/${detail.project.project_id}`} className="mt-1 inline-block text-sm font-medium text-primary hover:underline">
              {detail.project.project_name || detail.project.project_id}
            </Link>
          ) : (
            <p className="mt-1 text-sm text-muted-foreground">Unlinked</p>
          )}
        </div>
        <div>
          <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Assigned</p>
          <p className="mt-1 text-sm">{detail.assigned_team || "Desk / unassigned"}</p>
        </div>
      </section>

      {/* 2. Project Link */}
      <ProjectLinkBar project={detail.project} />

      {aiSummary ? (
        <Card className="border-primary/20 bg-primary/5">
          <CardHeader className="flex-row items-center justify-between gap-2 pb-2">
            <div className="flex items-center gap-2">
              <div className="flex size-6 items-center justify-center rounded-lg bg-primary/20">
                <span className="text-xs font-bold text-primary">AI</span>
              </div>
              <CardTitle className="text-sm text-primary">
                {summaryLoading ? "Refreshing analysis…" : "Current state"}
              </CardTitle>
            </div>
            {summaryError ? (
              <Button variant="ghost" size="sm" onClick={() => void loadSummary(true)}>
                Retry analysis
              </Button>
            ) : null}
          </CardHeader>
          <CardContent className="grid gap-4 text-sm md:grid-cols-3">
            <div>
              <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground mb-1">Likely Cause</p>
              <p className="text-sm">{aiSummary.what_happened}</p>
            </div>
            <div>
              <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground mb-1">Recommended</p>
              {aiSummary.recommended_action === "close_directly" && (
                <p className="text-sm font-medium text-success">✓ Close directly — client confirmed, no reply needed</p>
              )}
              {aiSummary.recommended_action === "investigate" && (
                <p className="text-sm font-medium text-warning">⚠ Investigate — more diagnostic info needed</p>
              )}
              {(!aiSummary.recommended_action || aiSummary.recommended_action === "send_reply") && (
                <p className="text-sm">{aiSummary.reply_draft_text ? "Draft reply prepared — review and send" : "Review conversation and determine next action"}</p>
              )}
            </div>
            <div>
              <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground mb-1">Parties</p>
              <p className="text-sm">{aiSummary.from_party} → {aiSummary.to_party}</p>
            </div>
          </CardContent>
        </Card>
      ) : null}

      {/* 4. Main 2-column: Conversation + Right Panel */}
      <div className="grid gap-4 md:grid-cols-2">
        <Card>
          <CardHeader className="flex-row items-center justify-between pb-0">
            <CardTitle className="text-sm">Conversation</CardTitle>
            <span className="rounded-full bg-muted px-2 py-0.5 text-xs font-semibold text-muted-foreground">
              {detail.conversation.length} messages
            </span>
          </CardHeader>
          <CardContent className="p-0">
            <ConversationThread messages={detail.conversation} />
          </CardContent>
        </Card>

        <div className="flex flex-col gap-4">
          <ContextPanel
            ball={detail.ball}
            followup={detail.followup}
            status={detail.status}
            draftText={draftText}
            onDraftChange={setDraftText}
            onRegenerate={handleRegenerate}
            onReviewSend={handleReviewSend}
            creatingDraft={creatingDraft}
          />
          <ActionButtons
            status={detail.status}
            ball={detail.ball.ball_with}
            loading={actionLoading}
            nextAction={aiSummary?.recommended_action === "close_directly" ? "close_stale" : undefined}
            onAction={handleAction}
            onAddNote={() => setNoteOpen(true)}
          />
          <Card>
            <CardHeader className="pb-0"><CardTitle className="text-sm">Details</CardTitle></CardHeader>
            <CardContent className="space-y-1.5 text-sm pt-3">
              <Row label="Category">{detail.category}</Row>
              <Row label="Source">{detail.source_type ?? "—"}</Row>
              {detail.counterparty && <Row label="Counterparty">{detail.counterparty}</Row>}
              {detail.counterparty_org_id && (
                <Row label="Party org">
                  <a
                    href={`/parties/${detail.counterparty_org_id}`}
                    className="text-primary hover:underline"
                  >
                    {detail.counterparty_org_name || detail.counterparty_org_id}
                  </a>
                </Row>
              )}
              {detail.reporter_email && <Row label="Reporter">{detail.reporter_email}</Row>}
              {detail.assigned_team && <Row label="Assigned">{detail.assigned_team}</Row>}
              {detail.created_at && <Row label="Created">{new Date(detail.created_at).toLocaleString()}</Row>}
              {detail.updated_at && <Row label="Updated">{new Date(detail.updated_at).toLocaleString()}</Row>}
            </CardContent>
          </Card>
        </div>
      </div>

      {/* 5. Activity Timeline */}
      <ActivityTimeline entries={detail.activity} />

      <Dialog open={noteOpen} onOpenChange={setNoteOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Add internal note</DialogTitle>
            <DialogDescription>Visible on this ticket only. It is not sent to the client.</DialogDescription>
          </DialogHeader>
          <Textarea value={noteText} onChange={(event) => setNoteText(event.target.value)} placeholder="What did we decide or check?" className="min-h-28" />
          <DialogFooter>
            <Button variant="outline" onClick={() => setNoteOpen(false)}>Cancel</Button>
            <Button disabled={!noteText.trim() || actionLoading} onClick={() => void handleAddNote()}>Save note</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Agent Chat drawer */}
      <AgentChatDrawer resourceType="support" resourceId={ticketId} />
    </div>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex justify-between">
      <span className="text-muted-foreground">{label}</span>
      <span>{children}</span>
    </div>
  );
}
