"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { ActivityTimeline } from "@/components/activity-timeline";
import { MilestoneTracker } from "@/components/milestone-tracker";
import { StatusBadge } from "@/components/status-badge";
import ProjectDraftPanel from "@/components/projects/project-draft-panel";
import FixSessionPanel from "@/components/projects/fix-session-panel";
import { ProjectCompletionApprovals } from "@/components/projects/project-completion-approvals";
import { ActionPlanPanel } from "@/components/projects/action-plan-panel";
import { UpdateNotificationsPanel } from "@/components/projects/update-notifications-panel";
import { ProjectBriefingCard } from "@/components/projects/project-briefing-card";
import { PageHeader } from "@/components/page-header";
import { ErrorPanel, LoadingPanel } from "@/components/state-panel";
import AgentChatDrawer from "@/components/support/agent-chat-drawer";
import { useAuth } from "@/lib/auth-context";
import {
  getProjectDraftSuggestion,
  getProjectFollowups,
  createProjectDraft,
  flagProject,
  saveProjectNotes,
  transferProjectOwner,
  markFollowupDone,
  requestProjectCompletion,
  sendToTelegram,
} from "@/lib/api";
import type { ActivityEntry } from "@/lib/types";
import { useProjectWorkspaceData } from "@/lib/use-project-workspace-data";
import { QuerySection } from "@/components/query-section";
import { useEvents, type SSEEvent } from "@/lib/useEvents";
import { notifyError, notifySuccess } from "@/lib/notifications";
import { ArrowLeft, CalendarDays, Clock, Mail, Send, Timer } from "lucide-react";

/** Project Recent Activity: only state truth + email spine (no TG/draft/operator noise). */
const ACTIVITY_FILTERS = ["All", "State", "Email"] as const;
type ActivityFilter = (typeof ACTIVITY_FILTERS)[number];

const STATE_ACTION_TYPES = [
  "truth_write",
  "auto_truth_write",
  "milestone_change",
  "milestone_correction",
  "pm_oracle_remediation",
  "project_update",
  "go_live_ready_notice",
] as const;

const EMAIL_ACTION_TYPES = [
  "inbound_reply",
  "email_sent",
  "email_received",
  "email_project_update_auto_processed",
  "email_support_ticket_created",
  "email_operator_notification_queued",
  "email_notification_suppressed",
] as const;

const FILTER_ACTION_TYPES: Record<ActivityFilter, readonly string[]> = {
  All: [...STATE_ACTION_TYPES, ...EMAIL_ACTION_TYPES],
  State: STATE_ACTION_TYPES,
  Email: EMAIL_ACTION_TYPES,
};

const PROJECT_REFRESH_EVENTS: string[] = [
  "project_email_processed",
  "project_updated",
  "review_approved",
  "review_rejected",
  "review_superseded",
];

function isProjectTimelineEntry(e: ActivityEntry, filter: ActivityFilter): boolean {
  return FILTER_ACTION_TYPES[filter].includes(e.action_type);
}

// ── NoteLog ──────────────────────────────────────────────────────────────────

const TAG_EMOJI: Record<string, string> = {
  email: "📧",
  orchestrator: "🤖",
  reopened: "🔄",
};

function tagEmoji(tag: string): string {
  const key = tag.toLowerCase();
  return TAG_EMOJI[key] ?? "👤";
}

interface NoteEntry {
  emoji: string;
  tag: string;
  date: string;   // e.g. "Feb 05"
  body: string;
}

function parseNotes(raw: string): NoteEntry[] {
  const tsPattern = /\[(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?)\]\s*$/;
  const tagPattern = /^\[([^\]]+)\]\s*/;

  return raw
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      const tsMatch = line.match(tsPattern);
      const date = tsMatch
        ? new Date(tsMatch[1]).toLocaleDateString("en-US", { month: "short", day: "2-digit" })
        : "";
      const withoutTs = tsMatch ? line.slice(0, line.lastIndexOf(tsMatch[0])).trim() : line;
      const tagMatch = withoutTs.match(tagPattern);
      const tag = tagMatch ? tagMatch[1] : "";
      const body = tagMatch ? withoutTs.slice(tagMatch[0].length).trim() : withoutTs;
      return { emoji: tagEmoji(tag), tag, date, body };
    });
}

function NoteLog({ notes }: { notes: string }) {
  const entries = parseNotes(notes);
  if (!entries.length) return null;
  return (
    <div className="max-h-56 overflow-y-auto rounded-md border bg-muted/30">
      {entries.map((e, i) => (
        <div
          key={i}
          className="flex items-start gap-2 border-b px-2 py-1.5 last:border-b-0 hover:bg-muted/60"
        >
          <span className="shrink-0 text-sm leading-tight">{e.emoji}</span>
          <span className="w-12 shrink-0 text-xs leading-tight text-muted-foreground">
            {e.date}
          </span>
          <span className="min-w-0 truncate text-xs leading-tight">{e.body}</span>
        </div>
      ))}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────

function formatRelativeTime(dateStr: string): string {
  const diff = Date.now() - new Date(dateStr).getTime();
  const hours = Math.floor(diff / (1000 * 60 * 60));
  if (hours < 1) return "< 1h ago";
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

export default function ProjectDetailPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const { hasPermission } = useAuth();
  const canWrite = hasPermission("business.write");
  const canCloseProject = hasPermission("project.close");

  const sections = useProjectWorkspaceData(id, canWrite);
  const project = sections.project.data;
  const activity = sections.activity.data ?? [];
  const evidence = sections.evidence.data ?? [];
  const contacts = sections.contacts.data ?? [];
  const parties = sections.parties.data?.parties ?? [];
  const deskIssues = sections.desk.data?.items ?? [];
  const { refresh: refreshProjectSnapshot, setProject } = sections;

  const refreshTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Draft panel
  const [draftText, setDraftText] = useState("");
  const [draftSubject, setDraftSubject] = useState("");
  const [draftLoading, setDraftLoading] = useState(false);
  const [creatingDraft, setCreatingDraft] = useState(false);

  // Notes
  // Refreshes must not overwrite an operator's unsaved notes.
  const [notesDraft, setNotesDraft] = useState<{ projectId: string; text: string } | null>(null);
  const notesText = notesDraft?.projectId === id ? notesDraft.text : project?.notes ?? "";
  const setNotesText = (text: string) => setNotesDraft({ projectId: id, text });
  const [notesSaving, setNotesSaving] = useState(false);
  const [notesSaved, setNotesSaved] = useState(false);
  const [quickAction, setQuickAction] = useState<string | null>(null);

  // Activity filter
  const [activityFilter, setActivityFilter] = useState<ActivityFilter>("All");
  const handleProjectEvent = useCallback((event: SSEEvent) => {
    if (!id || event.project_id !== id) return;
    if (refreshTimerRef.current) clearTimeout(refreshTimerRef.current);
    refreshTimerRef.current = setTimeout(() => {
      refreshProjectSnapshot().catch(() => {});
    }, 250);
  }, [id, refreshProjectSnapshot]);

  useEvents({
    eventTypes: PROJECT_REFRESH_EVENTS,
    onEvent: handleProjectEvent,
    onReconnect: () => { void refreshProjectSnapshot(); },
  });

  useEffect(() => {
    if (!id) return;
    let active = true;

    // Non-blocking Draft Suggestion is only available to users who can write.
    if (canWrite) {
      setDraftLoading(true);
      getProjectDraftSuggestion(id)
        .then((d) => { if (active) { setDraftText(d.draft_body); setDraftSubject(d.subject ?? ""); } })
        .catch(() => {})
        .finally(() => { if (active) setDraftLoading(false); });
    } else {
      setDraftLoading(false);
      setDraftText("");
      setDraftSubject("");
    }

    return () => {
      active = false;
      if (refreshTimerRef.current) clearTimeout(refreshTimerRef.current);
    };
  }, [id, canWrite]);

  const handleRegenerateSummary = () => {
    void sections.regenerateSummary().catch(() => {});
  };

  const handleRegenerateDraft = () => {
    if (!canWrite) return;
    setDraftLoading(true);
    getProjectDraftSuggestion(id)
      .then((d) => { setDraftText(d.draft_body); setDraftSubject(d.subject ?? ""); })
      .catch(() => {})
      .finally(() => setDraftLoading(false));
  };

  const handleReviewSend = async () => {
    if (!canWrite) return;
    setCreatingDraft(true);
    try {
      const data = await createProjectDraft(id, {
        draft_body: draftText,
        subject: draftSubject,
        task_type: "draft_email",
      });
      router.push(`/drafts/${data.draft_id}`);
    } catch (cause) {
      notifyError(cause, "Could not create the project draft.");
      // Keep draft text so the operator can retry.
    } finally {
      setCreatingDraft(false);
    }
  };

  const handleSaveNotes = async () => {
    if (!id || !canWrite) return;
    setNotesSaving(true);
    try {
      await saveProjectNotes(id, notesText);
      setNotesSaved(true);
      notifySuccess("Project notes saved");
      setTimeout(() => setNotesSaved(false), 2000);
    } catch (cause) {
      notifyError(cause, "Could not save project notes.");
    } finally {
      setNotesSaving(false);
    }
  };

  const handleSendToTelegram = async () => {
    try {
      await sendToTelegram(`Project ${project?.project_id ?? id} opened in dashboard`, id);
      notifySuccess("Project shared to Telegram");
    } catch (cause) {
      notifyError(cause, "Could not send the project to Telegram.");
    }
  };

  const [followupChoice, setFollowupChoice] = useState<{
    projectId: string;
    items: Array<{ followup_id: string; description: string; action_revision?: number }>;
  } | null>(null);
  const [selectedFollowup, setSelectedFollowup] = useState("");

  const completeChosenFollowup = async (item: { followup_id: string; action_revision?: number }, projectId: string) => {
    if (!canWrite || projectId !== id) return;
    setQuickAction("followup");
    try {
      if (!await markFollowupDone(item.followup_id, item.action_revision)) {
        throw new Error("The follow-up changed or could not be completed. Reload the list and review it again.");
      }
      notifySuccess("Follow-up completed");
      await refreshProjectSnapshot();
    } catch (cause) {
      notifyError(cause, "Could not complete the follow-up.");
    } finally {
      setFollowupChoice(null);
      setSelectedFollowup("");
      setQuickAction(null);
    }
  };

  const handleFollowupDone = async () => {
    setQuickAction("followup");
    try {
      const data = await getProjectFollowups(id);
      const open = (Array.isArray(data) ? data : []).filter((followup) => ["open", "overdue"].includes(followup.status));
      if (open.length === 0) {
        notifySuccess("No open follow-ups", "This project has no open follow-up to complete.");
        return;
      }
      const items = open.map((item) => ({
        followup_id: item.followup_id,
        description: typeof item.description === "string" ? item.description : item.followup_id,
        action_revision: typeof item.action_revision === "number" ? item.action_revision : undefined,
      }));
      if (items.length > 1) {
        setFollowupChoice({ projectId: id, items });
        setSelectedFollowup("");
        return;
      }
      await completeChosenFollowup(items[0], id);
    } catch (cause) {
      notifyError(cause, "Could not complete the follow-up.");
    } finally {
      setQuickAction(null);
    }
  };

  const handleFlag = async () => {
    setQuickAction("flag");
    try {
      await flagProject(id, "urgent");
      notifySuccess("Project flagged as urgent");
      await refreshProjectSnapshot();
    } catch (cause) {
      notifyError(cause, "Could not flag the project.");
    } finally {
      setQuickAction(null);
    }
  };

  const handleAddInternalNote = async () => {
    const note = window.prompt("Enter internal note:");
    if (!note?.trim()) return;
    setQuickAction("note");
    try {
      await saveProjectNotes(id, note.trim());
      notifySuccess("Internal note saved");
      await refreshProjectSnapshot();
    } catch (cause) {
      notifyError(cause, "Could not save the internal note.");
    } finally {
      setQuickAction(null);
    }
  };

  const handleSendCustomTelegram = async () => {
    const message = window.prompt("Message to send to Telegram:");
    if (!message?.trim()) return;
    setQuickAction("telegram");
    try {
      await sendToTelegram(message.trim(), id);
      notifySuccess("Message sent to Telegram");
    } catch (cause) {
      notifyError(cause, "Could not send the Telegram message.");
    } finally {
      setQuickAction(null);
    }
  };

  const handleTransferOwner = async () => {
    if (!id || !canWrite) return;
    const ownerUserId = window.prompt("Assign owner (Dashboard user id):", project?.operator_id || "");
    if (!ownerUserId?.trim()) return;
    setQuickAction("owner");
    try {
      const updated = await transferProjectOwner(id, ownerUserId.trim());
      setProject(updated);
      notifySuccess("Project owner updated");
    } catch (cause) {
      notifyError(cause, "Could not transfer the project owner.");
    } finally {
      setQuickAction(null);
    }
  };

  const handleRequestCompletion = async () => {
    if (!id || !canWrite || !canCloseProject) return;
    setQuickAction("completion");
    try {
      await requestProjectCompletion(id);
      notifySuccess(
        "Completion approval ready",
        "Another Editor must confirm completion on this page.",
      );
      await refreshProjectSnapshot();
    } catch (cause) {
      notifyError(cause, "Could not create the completion approval.");
    } finally {
      setQuickAction(null);
    }
  };

  const filteredActivity = activity.filter((e) =>
    isProjectTimelineEntry(e, activityFilter)
  );
  // Overview preview uses the same project-timeline rules as All
  const overviewActivity = activity.filter((e) =>
    isProjectTimelineEntry(e, "All")
  );

  if (sections.project.isError && !project) {
    return (
      <div className="mx-auto max-w-4xl py-8">
        <ErrorPanel description="Project details could not be loaded." onRetry={() => void sections.project.refetch()} />
      </div>
    );
  }
  if (!project) return <LoadingPanel label="Loading project workspace" />;

  return (
    <div className="mx-auto flex max-w-[1700px] flex-col gap-6">
      <PageHeader
        eyebrow="Projects"
        title={project.client_name ?? project.project_id}
        description="Review project truth, evidence, recent activity, parties, contacts, drafts, and FIX sessions."
        metadata={
          <>
            <span className="font-mono font-semibold text-primary">{project.project_id}</span>
            {project.market ? <span>{project.market}</span> : null}
            <StatusBadge value={project.project_status} />
            <StatusBadge value={project.ball_with} />
            <span>Owner {project.operator_id}</span>
            {project.current_milestone ? <span className="inline-flex items-center gap-1"><Timer className="size-3.5" />{project.current_milestone}</span> : null}
            {project.last_activity_at ? <span className="inline-flex items-center gap-1"><Clock className="size-3.5" />Last activity {formatRelativeTime(project.last_activity_at)}</span> : null}
            {project.created_at ? <span className="inline-flex items-center gap-1"><CalendarDays className="size-3.5" />Created {new Date(project.created_at).toLocaleDateString()}</span> : null}
          </>
        }
        actions={
          <>
            <Link href="/projects" className={buttonVariants({ variant: "outline" })}>
              <ArrowLeft className="size-4" />Back to projects
            </Link>
            {canWrite ? (
              <>
                <Button variant="outline" onClick={() => void handleTransferOwner()} disabled={quickAction === "owner"}>
                  Transfer owner
                </Button>
                <Button variant="outline" onClick={() => void handleSendToTelegram()}>
                  <Send className="size-4" />Send to Telegram
                </Button>
                <Button onClick={() => void handleReviewSend()} disabled={creatingDraft}>
                  <Mail className="size-4" />{creatingDraft ? "Creating..." : "Draft email"}
                </Button>
              </>
            ) : null}
          </>
        }
      />

      {sections.project.isError ? (
        <QuerySection label="Project details" query={sections.project}>{null}</QuerySection>
      ) : null}

      {canWrite && followupChoice?.projectId === id && (
        <Card className="mb-4">
          <CardHeader><CardTitle>Select the follow-up to complete</CardTitle></CardHeader>
          <CardContent className="space-y-3">
            <label className="block text-sm" htmlFor="followup-choice">Open follow-up</label>
            <select id="followup-choice" className="w-full rounded-md border bg-background p-2"
              value={selectedFollowup} disabled={quickAction === "followup"}
              onChange={(event) => setSelectedFollowup(event.target.value)}>
              <option value="">Choose a specific task</option>
              {followupChoice.items.map((item) => <option key={item.followup_id} value={item.followup_id}>{item.description} ({item.followup_id})</option>)}
            </select>
            <div className="flex gap-2">
              <Button disabled={!selectedFollowup || quickAction === "followup"} onClick={() => {
                const chosen = followupChoice.items.find((item) => item.followup_id === selectedFollowup);
                if (chosen) void completeChosenFollowup(chosen, followupChoice.projectId);
              }}>Complete selected follow-up</Button>
              <Button variant="outline" onClick={() => setFollowupChoice(null)}>Cancel selection</Button>
            </div>
          </CardContent>
        </Card>
      )}

      {canWrite ? <QuerySection label="Open desk issues" query={sections.desk}>
        {deskIssues.length ? (
          <section className="rounded-xl border bg-card p-4 shadow-xs" aria-label="Open desk issues">
            <div className="mb-3 flex items-center justify-between gap-3">
              <p className="text-sm font-semibold">Open desk issues</p>
              <Link href={`/support?q=${encodeURIComponent(id)}&queue=on_project`} className="text-xs text-primary hover:underline">
                View on desk
              </Link>
            </div>
            <ul className="space-y-2">
              {deskIssues.map((item) => (
                <li key={item.ticket_id} className="flex flex-wrap items-center justify-between gap-2 text-sm">
                  <Link href={`/support/${item.ticket_id}`} className="font-medium text-primary hover:underline">
                    {item.display_title || item.title}
                  </Link>
                  <span className="text-xs text-muted-foreground">
                    {item.next_action.replaceAll("_", " ")} · {item.age_days ?? 0}d
                  </span>
                </li>
              ))}
            </ul>
          </section>
        ) : null}

      </QuerySection> : null}

      {/* Main body */}
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-5">
        {/* LEFT: Tabs (3/5) */}
        <div className="min-w-0 lg:col-span-3">
          <Tabs defaultValue="overview">
            <TabsList aria-label="Project views" className="h-auto w-full flex-wrap justify-start">
              <TabsTrigger value="overview">Overview</TabsTrigger>
              <TabsTrigger value="actions">Action plan</TabsTrigger>
              <TabsTrigger value="activity">Activity</TabsTrigger>
              <TabsTrigger value="evidence">Evidence</TabsTrigger>
              <TabsTrigger value="notes">Notes</TabsTrigger>
              {canWrite && <TabsTrigger value="drafts">Drafts</TabsTrigger>}
              <TabsTrigger value="sessions">FIX Sessions</TabsTrigger>
              <TabsTrigger value="notifications">Notifications</TabsTrigger>
            </TabsList>

            <TabsContent value="actions" className="mt-4">
              <ActionPlanPanel key={project.project_id} projectId={project.project_id} />
            </TabsContent>

            <TabsContent value="notifications" className="mt-4">
              <UpdateNotificationsPanel key={project.project_id} projectId={project.project_id} />
            </TabsContent>

            {/* Overview Tab */}
            <TabsContent value="overview" className="mt-4 space-y-4">
              {/* Milestone Tracker */}
              <MilestoneTracker currentMilestone={project.current_milestone} />
              {canCloseProject ? <ProjectCompletionApprovals projectId={project.project_id} /> : null}

              {/* Evidence-aware AI Summary */}
              <QuerySection label="AI summary" query={sections.summary}>
                <ProjectBriefingCard
                  summary={sections.summary.data ?? null}
                  loading={sections.summary.isFetching}
                  onRegenerate={handleRegenerateSummary}
                />
              </QuerySection>

              {/* Recent Activity Preview */}
              <div>
                <p className="mb-2 text-xs font-semibold uppercase tracking-widest text-muted-foreground">Recent Activity</p>
                <QuerySection label="Recent activity" query={sections.activity}>
                  <ActivityTimeline entries={overviewActivity.slice(0, 5)} />
                </QuerySection>
              </div>
            </TabsContent>

            {/* Activity Tab */}
            <TabsContent value="activity" className="mt-4">
              <div className="mb-3 flex flex-wrap gap-2">
                {ACTIVITY_FILTERS.map((f) => (
                  <button
                    key={f}
                    onClick={() => setActivityFilter(f)}
                    className={`rounded-full px-3 py-1 text-xs font-medium transition-colors ${
                      activityFilter === f
                        ? "bg-primary text-primary-foreground"
                        : "bg-muted text-muted-foreground hover:bg-accent"
                    }`}
                  >
                    {f}
                  </button>
                ))}
              </div>
              <QuerySection label="Project activity" query={sections.activity}>
                <ActivityTimeline entries={filteredActivity} />
              </QuerySection>
            </TabsContent>

            {/* Evidence Tab */}
            <TabsContent value="evidence" className="mt-4">
              <QuerySection label="Evidence" query={sections.evidence}>
                {evidence.length === 0 ? (
                  <p className="text-sm text-muted-foreground">No evidence yet.</p>
                ) : (
                  <div className="flex flex-col gap-3">
                    {evidence.map((e) => (
                      <Card key={e.evidence_id}>
                        <CardContent className="pb-3 pt-3">
                          <div className="mb-1 flex items-center gap-2">
                            <Badge variant="outline" className="text-xs">{e.evidence_type}</Badge>
                            <span className="text-xs text-muted-foreground">{new Date(e.evidence_at).toLocaleString()}</span>
                          </div>
                          <p className="text-sm">{e.content_summary}</p>
                          {e.source_ref && <p className="mt-1 text-xs text-muted-foreground">Source: {e.source_ref}</p>}
                        </CardContent>
                      </Card>
                    ))}
                  </div>
                )}
            </QuerySection>
            </TabsContent>

            {/* Notes Tab */}
            <TabsContent value="notes" className="mt-4">
              <Textarea
                value={notesText}
                onChange={canWrite ? (e) => setNotesText(e.target.value) : undefined}
                readOnly={!canWrite}
                placeholder={canWrite ? "Add notes about this project..." : "No notes recorded."}
                className="min-h-[200px] resize-y font-mono text-sm read-only:cursor-default read-only:bg-muted/30"
              />
              {canWrite && (
                <div className="mt-2 flex items-center gap-2">
                  <Button size="sm" onClick={() => void handleSaveNotes()} disabled={notesSaving}>
                    {notesSaving ? "Saving..." : "Save Notes"}
                  </Button>
                  {notesSaved && <span className="text-xs text-[var(--success)]">Saved</span>}
                </div>
              )}
            </TabsContent>

            {/* Drafts Tab */}
            {canWrite && (
              <TabsContent value="drafts" className="mt-4">
                <ProjectDraftPanel
                  ballWith={project.ball_with}
                  draftText={draftText}
                  draftSubject={draftSubject}
                  loading={draftLoading}
                  onDraftChange={setDraftText}
                  onSubjectChange={setDraftSubject}
                  onRegenerate={handleRegenerateDraft}
                  onReviewSend={handleReviewSend}
                  creatingDraft={creatingDraft}
                />
              </TabsContent>
            )}

            {/* Canonical FIX Sessions Tab */}
            <TabsContent value="sessions" className="mt-4">
              <FixSessionPanel projectId={id} />
            </TabsContent>
          </Tabs>
        </div>

        {/* RIGHT: Project Details + Actions (2/5) */}
        <div className="flex min-w-0 flex-col gap-4 lg:col-span-2">
          {/* Project Details */}
          <Card>
            <CardHeader className="pb-2 pt-4">
              <CardTitle className="text-sm font-semibold">Project Details</CardTitle>
            </CardHeader>
            <CardContent className="space-y-2.5 text-sm">
              {project.client_name && (
                <div className="flex items-start justify-between gap-2">
                  <span className="shrink-0 text-muted-foreground">Contact</span>
                  <div className="text-right">
                    <div className="font-medium">{project.client_name}</div>
                    {project.client_email && (
                      <div className="text-xs text-primary">{project.client_email}</div>
                    )}
                  </div>
                </div>
              )}
              {project.connection_type && (
                <div className="flex items-center justify-between gap-2">
                  <span className="text-muted-foreground">Connection</span>
                  <span className="font-medium">{project.connection_type}</span>
                </div>
              )}
              <div className="flex items-center justify-between gap-2">
                <span className="text-muted-foreground">Market</span>
                <span className="font-mono text-xs font-semibold text-primary">{project.market ?? "—"}</span>
              </div>
              <div className="flex items-center justify-between gap-2">
                <span className="text-muted-foreground">Project ID</span>
                <span className="font-mono text-xs font-semibold text-primary">{project.project_id}</span>
              </div>
              <div className="flex items-center justify-between gap-2">
                <span className="text-muted-foreground">Ball With</span>
                <span className="font-medium">{project.ball_with}</span>
              </div>
              {project.sla_limit != null && (
                <div className="flex items-center justify-between gap-2">
                  <span className="text-muted-foreground">SLA Days</span>
                  <span className="font-medium">{project.sla_limit}</span>
                </div>
              )}
              {project.broker_code && (
                <div className="flex items-center justify-between gap-2">
                  <span className="text-muted-foreground">Broker Code</span>
                  <span className="font-mono text-xs">{project.broker_code}</span>
                </div>
              )}
              {project.target_go_live && (
                <div className="flex items-center justify-between gap-2">
                  <span className="text-muted-foreground">Target Go-Live</span>
                  <span className="font-medium">{project.target_go_live}</span>
                </div>
              )}
              {project.notes && (
                <div className="pt-1">
                  <span className="mb-1 block text-xs text-muted-foreground">Notes</span>
                  <NoteLog notes={project.notes} />
                </div>
              )}
            </CardContent>
          </Card>

          {/* Actions */}
          {canWrite && (
          <Card>
            <CardHeader className="pb-2 pt-3">
              <CardTitle className="text-sm">Actions</CardTitle>
            </CardHeader>
            <CardContent className="flex flex-col gap-2">
              <Button variant="outline" size="sm" className="justify-start" disabled={quickAction !== null} onClick={() => void handleFollowupDone()}>
                {quickAction === "followup" ? "Completing..." : "Mark follow-up done"}
              </Button>
              <Button variant="outline" size="sm" className="justify-start" disabled={quickAction !== null} onClick={() => void handleFlag()}>
                {quickAction === "flag" ? "Flagging..." : "Escalate / flag"}
              </Button>
              <Button variant="outline" size="sm" className="justify-start" disabled={quickAction !== null} onClick={() => void handleAddInternalNote()}>
                {quickAction === "note" ? "Saving..." : "Add internal note"}
              </Button>
              <Button variant="outline" size="sm" className="justify-start" disabled={quickAction !== null} onClick={() => void handleSendCustomTelegram()}>
                {quickAction === "telegram" ? "Sending..." : "Send to Telegram"}
              </Button>
            </CardContent>
          </Card>
          )}

          {canWrite
            && canCloseProject
            && !["Completed", "Archived", "Cancelled"].includes(project.project_status)
            && project.current_milestone === "M8_GoLive" && (
            <Card className="border-primary/20">
              <CardHeader className="pb-2 pt-3">
                <CardTitle className="text-sm">Project lifecycle</CardTitle>
              </CardHeader>
              <CardContent className="space-y-3">
                <p className="text-xs leading-5 text-muted-foreground">
                  Create a version-bound approval, then confirm completion in the Approval Center.
                </p>
                <Button
                  variant="outline"
                  size="sm"
                  className="w-full justify-start border-primary/30 text-primary hover:bg-primary/5"
                  disabled={quickAction !== null}
                  onClick={() => void handleRequestCompletion()}
                >
                  {quickAction === "completion" ? "Creating approval..." : "Request completion approval"}
                </Button>
              </CardContent>
            </Card>
          )}

          {/* Parties card (client / vendor / internal orgs) */}
          <QuerySection label="Parties" query={sections.parties}>
            {parties.length > 0 && (
              <Card>
                <CardHeader className="pb-2 pt-4">
                  <CardTitle className="text-sm font-semibold">Parties</CardTitle>
                </CardHeader>
                <CardContent className="space-y-2 pb-4">
                  {parties.map((p) => (
                    <Link
                      key={`${p.org_id}-${p.party_role}`}
                      href={`/parties/${p.org_id}`}
                      className="flex items-start justify-between gap-2 rounded-md px-1 py-1 text-sm hover:bg-muted/50"
                    >
                      <div className="min-w-0">
                        <div className="truncate font-medium">{p.org_name ?? p.org_id}</div>
                        <div className="text-xs text-muted-foreground">
                          {p.domains || "—"}
                        </div>
                      </div>
                      <div className="flex shrink-0 flex-col items-end gap-0.5">
                        <Badge variant="outline" className="text-xs capitalize">
                          {p.party_role}
                        </Badge>
                        {p.org_type && p.org_type !== p.party_role && (
                          <span className="text-xs text-muted-foreground">{p.org_type}</span>
                        )}
                      </div>
                    </Link>
                  ))}
                </CardContent>
              </Card>
            )}
          </QuerySection>

          {/* Contacts card */}
          <QuerySection label="Contacts" query={sections.contacts}>
            {contacts.length > 0 && (
              <Card>
                <CardHeader className="pb-2 pt-4">
                  <CardTitle className="text-sm font-semibold">Contacts</CardTitle>
                </CardHeader>
                <CardContent className="space-y-2 pb-4">
                  {contacts.map((c) => (
                    <div key={c.contact_id} className="flex items-start justify-between gap-2 text-sm">
                      <div className="min-w-0">
                        <div className="truncate font-medium">{c.display_name}</div>
                        {c.role && (
                          <div className="text-xs text-muted-foreground">{c.role}</div>
                        )}
                      </div>
                      <a
                        href={`mailto:${c.primary_email}`}
                        className="shrink-0 text-xs text-primary hover:underline"
                      >
                        {c.primary_email}
                      </a>
                    </div>
                  ))}
                </CardContent>
              </Card>
            )}
          </QuerySection>

        </div>
      </div>

      {/* Floating Ask Agent Drawer */}
      <AgentChatDrawer resourceType="project" resourceId={id} />
    </div>
  );
}
