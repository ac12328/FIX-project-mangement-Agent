"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { FileCheck2, History, Loader2, PanelRightOpen } from "lucide-react";

import { WorkspaceComposer } from "@/components/ai-workspace/composer";
import { ContextPanel, type ContextPanelTab } from "@/components/ai-workspace/context-panel";
import { MessageFeed } from "@/components/ai-workspace/message-feed";
import { ThreadSidebar } from "@/components/ai-workspace/thread-sidebar";
import { HelpDialog, ProjectPickerDialog, ResumeDialog } from "@/components/ai-workspace/workspace-dialogs";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import {
  createAssistantThread,
  deleteAssistantThread,
  getAssistantCapabilities,
  getAssistantThread,
  listAssistantMessages,
  listAssistantThreads,
  patchAssistantThread,
  streamAssistantTurn,
} from "@/lib/assistant-api";
import type {
  AssistantCapabilities,
  AssistantMessage,
  AssistantThread,
  AssistantThreadPatch,
} from "@/lib/assistant-types";
import {
  buildTurnPayload,
  EMPTY_STREAMING_TURN,
  findRegenerationPrompt,
  isKnownWorkspaceCommand,
  parseWorkspaceCommand,
  reduceStreamingTurn,
  type StreamingTurnState,
} from "@/lib/assistant-workspace";
import { ApiError, getProjects } from "@/lib/api";
import { useEvents } from "@/lib/useEvents";
import {
  selectAssistantSources,
  selectLatestAssistantFollowUpQuestions,
  selectPendingAssistantActions,
} from "@/lib/assistant-workspace-parts";
import type { ProjectSummary } from "@/lib/types";
import { cn } from "@/lib/utils";

const PORTFOLIO_STARTERS = [
  "/status",
  "Show blocked projects and explain the common themes.",
  "Which projects need attention this week?",
  "List internal projects in a table.",
] as const;
const SIDEBAR_STORAGE_KEY = "ai-workspace:history-collapsed";

const PROJECT_STARTERS = [
  "/status",
  "Summarize the current risks and next actions.",
  "What is blocking progress?",
  "/draft Draft a concise client update.",
] as const;

function messageForError(error: unknown) {
  if (error instanceof ApiError && error.status === 409) return error.message;
  return error instanceof Error ? error.message : "The AI Workspace request could not be completed.";
}

function replaceThread(items: AssistantThread[], updated: AssistantThread) {
  return [updated, ...items.filter((item) => item.thread_id !== updated.thread_id)];
}

export function AiWorkspaceShell() {
  const [capabilities, setCapabilities] = useState<AssistantCapabilities | null>(null);
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [threads, setThreads] = useState<AssistantThread[]>([]);
  const [threadCursor, setThreadCursor] = useState<string | null>(null);
  const [selectedThread, setSelectedThread] = useState<AssistantThread | null>(null);
  const [messages, setMessages] = useState<AssistantMessage[]>([]);
  const [messageCursor, setMessageCursor] = useState<string | null>(null);
  const [streaming, setStreaming] = useState<StreamingTurnState>(EMPTY_STREAMING_TURN);
  const [input, setInput] = useState("");
  const [search, setSearch] = useState("");
  const [archived, setArchived] = useState(false);
  const [loading, setLoading] = useState(true);
  const [threadsLoading, setThreadsLoading] = useState(false);
  const [messagesLoading, setMessagesLoading] = useState(false);
  const [loadingMoreThreads, setLoadingMoreThreads] = useState(false);
  const [loadingEarlier, setLoadingEarlier] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [activity, setActivity] = useState<string[]>([]);
  const [resumeOpen, setResumeOpen] = useState(false);
  const [resumeQuery, setResumeQuery] = useState("");
  const [helpOpen, setHelpOpen] = useState(false);
  const [projectPicker, setProjectPicker] = useState<"scope" | "compare" | null>(null);
  const [projectQuery, setProjectQuery] = useState("");
  const [pendingCompare, setPendingCompare] = useState<string | null>(null);
  const [mobileHistoryOpen, setMobileHistoryOpen] = useState(false);
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [detailsTab, setDetailsTab] = useState<ContextPanelTab>("context");
  const [detailsSourceIds, setDetailsSourceIds] = useState<string[] | null>(null);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const initializedRef = useRef(false);

  const projectLabels = useMemo(() => Object.fromEntries(
    projects.map((project) => [project.project_id, project.client_name || project.project_id]),
  ), [projects]);
  const sources = useMemo(
    () => selectAssistantSources(messages, streaming.parts),
    [messages, streaming.parts],
  );
  const pendingActions = useMemo(
    () => selectPendingAssistantActions(messages, streaming.parts),
    [messages, streaming.parts],
  );
  const followUpQuestions = useMemo(
    () => streaming.status === "running"
      ? []
      : selectLatestAssistantFollowUpQuestions(
        messages,
        streaming.status === "completed" ? streaming.parts : [],
      ),
    [messages, streaming.parts, streaming.status],
  );

  useEffect(() => {
    setSidebarCollapsed(window.localStorage.getItem(SIDEBAR_STORAGE_KEY) === "true");
  }, []);

  const updateSidebarCollapsed = useCallback((collapsed: boolean) => {
    setSidebarCollapsed(collapsed);
    window.localStorage.setItem(SIDEBAR_STORAGE_KEY, String(collapsed));
  }, []);

  const loadMessages = useCallback(async (threadId: string, cursor?: string, prepend = false) => {
    if (prepend) setLoadingEarlier(true);
    else setMessagesLoading(true);
    try {
      const page = await listAssistantMessages(threadId, { limit: 80, cursor });
      setMessages((current) => prepend ? [...page.items, ...current] : page.items);
      setMessageCursor(page.next_cursor);
    } finally {
      if (prepend) setLoadingEarlier(false);
      else setMessagesLoading(false);
    }
  }, []);

  const refreshWorkspace = useCallback(() => {
    if (selectedThread) {
      void loadMessages(selectedThread.thread_id).catch((caught) => setError(messageForError(caught)));
    }
    void getProjects().then(setProjects).catch(() => undefined);
  }, [loadMessages, selectedThread]);

  useEvents({
    eventTypes: ["project_updated"],
    onRefresh: refreshWorkspace,
  });

  const selectThread = useCallback((thread: AssistantThread) => {
    abortRef.current?.abort();
    setSelectedThread(thread);
    setStreaming(EMPTY_STREAMING_TURN);
    setError(null);
    setMobileHistoryOpen(false);
    setDetailsSourceIds(null);
    setDetailsTab("context");
    window.history.replaceState(null, "", `/ask?thread=${encodeURIComponent(thread.thread_id)}`);
    void loadMessages(thread.thread_id).catch((caught) => setError(messageForError(caught)));
  }, [loadMessages]);

  const refreshThreadList = useCallback(async (options?: { append?: boolean; cursor?: string }) => {
    if (options?.append) setLoadingMoreThreads(true);
    else setThreadsLoading(true);
    try {
      const page = await listAssistantThreads({ search, archived, limit: 50, cursor: options?.cursor });
      setThreads((current) => options?.append ? [...current, ...page.items] : page.items);
      setThreadCursor(page.next_cursor);
      return page.items;
    } finally {
      if (options?.append) setLoadingMoreThreads(false);
      else setThreadsLoading(false);
    }
  }, [archived, search]);

  const createNewThread = useCallback(async (projectIds: string[] = []) => {
    const thread = await createAssistantThread(projectIds.length
      ? { scope_mode: "projects", project_ids: projectIds }
      : { scope_mode: "portfolio" });
    setArchived(false);
    setSearch("");
    setThreads((current) => [thread, ...current.filter((item) => item.thread_id !== thread.thread_id)]);
    setMessages([]);
    setMessageCursor(null);
    selectThread(thread);
    return thread;
  }, [selectThread]);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      setLoading(true);
      try {
        const capabilitiesResult = await getAssistantCapabilities();
        if (cancelled) return;
        setCapabilities(capabilitiesResult);
        if (!capabilitiesResult.enabled) throw new Error("AI Workspace is disabled by configuration.");
        const [projectResult, threadPage] = await Promise.all([
          getProjects().catch(() => [] as ProjectSummary[]),
          listAssistantThreads({ limit: 50 }),
        ]);
        if (cancelled) return;
        setProjects(projectResult);
        setThreads(threadPage.items);
        setThreadCursor(threadPage.next_cursor);
        const requestedId = new URLSearchParams(window.location.search).get("thread");
        let initial = requestedId
          ? await getAssistantThread(requestedId).catch(() => null)
          : threadPage.items[0] || null;
        if (!initial) initial = await createAssistantThread({ scope_mode: "portfolio" });
        if (cancelled) return;
        setSelectedThread(initial);
        window.history.replaceState(null, "", `/ask?thread=${encodeURIComponent(initial.thread_id)}`);
        await loadMessages(initial.thread_id);
        initializedRef.current = true;
      } catch (caught) {
        if (!cancelled) setError(messageForError(caught));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; abortRef.current?.abort(); };
  }, [loadMessages]);

  useEffect(() => {
    if (!initializedRef.current) return;
    const timer = window.setTimeout(() => {
      void refreshThreadList().catch((caught) => setError(messageForError(caught)));
    }, search ? 220 : 0);
    return () => window.clearTimeout(timer);
  }, [archived, refreshThreadList, search]);

  async function patchCurrent(patch: AssistantThreadPatch) {
    if (!selectedThread) return null;
    const updated = await patchAssistantThread(selectedThread.thread_id, patch);
    setSelectedThread(updated);
    setThreads((current) => replaceThread(current, updated));
    await loadMessages(updated.thread_id);
    return updated;
  }

  async function sendAgentTurn(text: string, thread = selectedThread) {
    if (!thread || streaming.status === "running") return;
    setError(null);
    const payload = buildTurnPayload(text, thread);
    const optimistic: AssistantMessage = {
      message_id: `local-${payload.client_turn_id}`,
      thread_id: thread.thread_id,
      role: "user",
      content: text,
      parts: [{ type: "text", text }],
      created_at: new Date().toISOString(),
    };
    setMessages((current) => [...current, optimistic]);
    setStreaming({ ...EMPTY_STREAMING_TURN, status: "running", progress: "Sending request…" });
    setActivity((current) => [...current, "Request sent to the assistant."]);
    const controller = new AbortController();
    abortRef.current = controller;
    let serverTurnId: string | null = null;
    try {
      await streamAssistantTurn(thread.thread_id, payload, {
        signal: controller.signal,
        onEvent: (event) => {
          serverTurnId = event.data.turn_id;
          setStreaming((current) => reduceStreamingTurn(current, event));
          if (event.event === "progress") setActivity((current) => [...current, event.data.message]);
          if (event.event === "part.added") setActivity((current) => [...current, `${event.data.part.type.replaceAll("_", " ")} added.`]);
        },
        onReconnect: () => {
          setStreaming({ ...EMPTY_STREAMING_TURN, status: "running", progress: "Reconnecting to the running request…" });
          setActivity((current) => [...current, "Stream interrupted; reconnecting to the running turn."]);
        },
      });
      const [page, updated] = await Promise.all([
        listAssistantMessages(thread.thread_id, { limit: 80 }),
        getAssistantThread(thread.thread_id),
      ]);
      setMessages(page.items);
      setMessageCursor(page.next_cursor);
      setSelectedThread(updated);
      setThreads((current) => [updated, ...current.filter((item) => item.thread_id !== updated.thread_id)]);
      setStreaming(EMPTY_STREAMING_TURN);
      setActivity((current) => [...current, "Turn completed and saved."]);
    } catch (caught) {
      const stopped = caught instanceof DOMException && caught.name === "AbortError";
      try {
        const [page, updated] = await Promise.all([
          listAssistantMessages(thread.thread_id, { limit: 80 }),
          getAssistantThread(thread.thread_id),
        ]);
        setMessages(page.items);
        setMessageCursor(page.next_cursor);
        setSelectedThread(updated);
        setThreads((current) => [updated, ...current.filter((item) => item.thread_id !== updated.thread_id)]);
        const terminalSaved = Boolean(serverTurnId && page.items.some(
          (item) => item.turn_id === serverTurnId && item.role === "assistant",
        ));
        if (terminalSaved) {
          setStreaming(EMPTY_STREAMING_TURN);
          setActivity((current) => [...current, "Recovered the saved result after the stream disconnected."]);
        } else {
          const message = stopped
            ? "Stopped following this request. It may continue in the background; reload the thread to recover the result."
            : "Connection lost. The request may still be running in the background; reload the thread to recover the result.";
          setStreaming((current) => ({ ...current, status: "failed", progress: null, error: message }));
          setError(message);
          setActivity((current) => [...current, message]);
        }
      } catch {
        const message = stopped
          ? "Stopped following this request. It may continue in the background."
          : messageForError(caught);
        setStreaming((current) => ({ ...current, status: "failed", progress: null, error: message }));
        setError(message);
      }
    } finally {
      abortRef.current = null;
    }
  }

  async function submit() {
    const text = input.trim();
    if (!text || !selectedThread) return;
    const parsed = parseWorkspaceCommand(text);
    if (parsed && !isKnownWorkspaceCommand(parsed.name)) {
      setError(`Unknown command: /${parsed.name}. Type /help to see available commands.`);
      return;
    }
    setInput("");
    if (parsed?.name === "new") return void createNewThread().catch((caught) => setError(messageForError(caught)));
    if (parsed?.name === "resume") {
      setResumeQuery(parsed.arguments);
      return setResumeOpen(true);
    }
    if (parsed?.name === "project") {
      setProjectQuery(parsed.arguments);
      return setProjectPicker("scope");
    }
    if (parsed?.name === "help") return setHelpOpen(true);
    if (parsed?.name === "portfolio") return void patchCurrent({ scope_mode: "portfolio", project_ids: [] }).catch((caught) => setError(messageForError(caught)));
    if (parsed?.name === "web") {
      if (!capabilities?.web_search_available) { setError(capabilities?.web_search_reason || "Web Search is unavailable."); return; }
      if (!['on', 'off'].includes(parsed.arguments.toLowerCase())) { setError("Use /web on or /web off."); return; }
      return void patchCurrent({ web_enabled: parsed.arguments.toLowerCase() === "on" }).catch((caught) => setError(messageForError(caught)));
    }
    if (parsed?.name === "compare" && selectedThread.project_ids.length < 2) {
      setPendingCompare(text);
      setProjectQuery("");
      setProjectPicker("compare");
      return;
    }
    void sendAgentTurn(text);
  }

  async function applyProjects(projectIds: string[]) {
    try {
      const updated = await patchCurrent({ scope_mode: "projects", project_ids: projectIds });
      if (projectPicker === "compare" && pendingCompare && updated) {
        const command = pendingCompare;
        setPendingCompare(null);
        await sendAgentTurn(command, updated);
      }
    } catch (caught) {
      setError(messageForError(caught));
    }
  }

  async function archiveThread(thread: AssistantThread, shouldArchive: boolean) {
    try {
      const updated = await patchAssistantThread(thread.thread_id, { archived: shouldArchive });
      setThreads((current) => current.filter((item) => item.thread_id !== thread.thread_id));
      if (selectedThread?.thread_id === thread.thread_id && shouldArchive) {
        const remaining = threads.filter((item) => item.thread_id !== thread.thread_id && !item.archived_at);
        if (remaining[0]) selectThread(remaining[0]); else await createNewThread();
      } else if (!shouldArchive) setThreads((current) => [updated, ...current]);
    } catch (caught) { setError(messageForError(caught)); }
  }

  async function removeThread(thread: AssistantThread) {
    await deleteAssistantThread(thread.thread_id);
    const remaining = threads.filter((item) => item.thread_id !== thread.thread_id);
    setThreads(remaining);
    if (selectedThread?.thread_id === thread.thread_id) {
      setDetailsOpen(false);
      setDetailsSourceIds(null);
      if (remaining[0]) selectThread(remaining[0]);
      else await createNewThread();
    }
  }

  function regenerateResponse(message: AssistantMessage) {
    const prompt = findRegenerationPrompt(messages, message);
    if (!prompt) {
      setError("The original prompt for this response is no longer available.");
      return;
    }
    void sendAgentTurn(prompt);
  }

  function showResponseSources(message: AssistantMessage) {
    setDetailsSourceIds(message.parts.flatMap((part) => part.type === "source" ? [String(part.source_id)] : []));
    setDetailsTab("sources");
    setDetailsOpen(true);
  }

  function openWorkspaceDetails() {
    setDetailsSourceIds(null);
    setDetailsTab("context");
    setDetailsOpen(true);
  }

  function askFollowUpQuestion(question: string) {
    void sendAgentTurn(question);
  }

  function openPendingActions() {
    setDetailsSourceIds(null);
    setDetailsTab("actions");
    setDetailsOpen(true);
  }

  if (loading) return <div className="flex h-full min-h-0 items-center justify-center gap-3 text-sm text-muted-foreground"><Loader2 className="size-5 animate-spin" />Opening AI Workspace…</div>;
  if (!selectedThread || !capabilities) return <div className="flex h-full min-h-0 items-center justify-center p-6 text-center text-sm text-destructive">{error || "AI Workspace is unavailable."}</div>;

  const sidebarProps = {
    threads,
    selectedThreadId: selectedThread.thread_id,
    search,
    archived,
    loading: threadsLoading,
    loadingMore: loadingMoreThreads,
    hasMore: Boolean(threadCursor),
    onSearch: setSearch,
    onArchivedChange: setArchived,
    onSelect: selectThread,
    onNew: () => void createNewThread(),
    onRename: (thread: AssistantThread, title: string) => void patchAssistantThread(thread.thread_id, { title }).then((updated) => {
      setThreads((current) => current.map((item) => item.thread_id === updated.thread_id ? updated : item));
      if (selectedThread.thread_id === updated.thread_id) setSelectedThread(updated);
    }).catch((caught) => setError(messageForError(caught))),
    onArchive: (thread: AssistantThread, value: boolean) => void archiveThread(thread, value),
    onDelete: removeThread,
    onLoadMore: () => void refreshThreadList({ append: true, cursor: threadCursor || undefined }),
  };
  const detailsLabel = `Open workspace details, ${sources.length} ${sources.length === 1 ? "source" : "sources"}, ${pendingActions.length} pending ${pendingActions.length === 1 ? "action" : "actions"}`;

  return (
    <div className="flex h-full min-h-0 overflow-hidden bg-background">
      <div className={cn(
        "hidden shrink-0 border-r transition-[width] duration-200 motion-reduce:transition-none min-[1200px]:block",
        sidebarCollapsed ? "w-14" : "w-64",
      )}>
        <ThreadSidebar {...sidebarProps} collapsed={sidebarCollapsed} collapsible onCollapsedChange={updateSidebarCollapsed} />
      </div>
      <section data-testid="assistant-conversation" className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
        <header className="flex min-h-14 items-center gap-2 border-b bg-card/80 px-3 backdrop-blur sm:px-5">
          <Button
            type="button"
            size="icon-sm"
            variant="ghost"
            className="min-[1200px]:hidden"
            aria-label="Open conversation history"
            onClick={() => setMobileHistoryOpen(true)}
          >
            <History />
          </Button>
          <div className="min-w-0 flex-1">
            <h1 className="truncate font-heading text-sm font-semibold sm:text-base">{selectedThread.title}</h1>
          </div>
          <Button
            type="button"
            size="icon-sm"
            variant="ghost"
            aria-label={detailsLabel}
            aria-haspopup="dialog"
            aria-expanded={detailsOpen}
            onClick={openWorkspaceDetails}
            className="relative"
          >
            <PanelRightOpen className="size-4" />
            {pendingActions.length ? (
              <span className="absolute right-0.5 top-0.5 flex min-w-4 items-center justify-center rounded-full bg-primary px-1 text-[9px] font-semibold leading-4 text-primary-foreground">
                {pendingActions.length > 9 ? "9+" : pendingActions.length}
              </span>
            ) : null}
          </Button>
        </header>
        {error ? <div role="alert" className="flex items-center justify-between gap-3 border-b border-destructive/25 bg-destructive/5 px-4 py-2 text-sm text-destructive"><span>{error}</span><button type="button" className="min-h-8 rounded-md px-2 font-medium hover:bg-destructive/10" onClick={() => setError(null)}>Dismiss</button></div> : null}
        <div data-testid="assistant-message-scroll" className="min-h-0 flex-1 overflow-y-auto">
          <MessageFeed
            messages={messages}
            streaming={streaming}
            loading={messagesLoading}
            loadingEarlier={loadingEarlier}
            hasEarlier={Boolean(messageCursor)}
            canManageActions={capabilities.can_create_actions}
            operatorWrites={Boolean(capabilities.operator_writes)}
            onResolved={refreshWorkspace}
            starterPrompts={selectedThread.scope_mode === "portfolio" ? PORTFOLIO_STARTERS : PROJECT_STARTERS}
            onStarter={setInput}
            onLoadEarlier={() => void loadMessages(selectedThread.thread_id, messageCursor || undefined, true)}
            onActivity={(entry) => setActivity((current) => [...current, entry])}
            onRegenerate={regenerateResponse}
            onShowSources={showResponseSources}
            regenerateDisabled={streaming.status === "running" || Boolean(selectedThread.archived_at)}
            followUpQuestions={followUpQuestions}
            followUpDisabled={Boolean(selectedThread.archived_at)}
            onFollowUp={askFollowUpQuestion}
          />
        </div>
        {pendingActions.length ? (
          <div className="border-t border-warning/30 bg-warning/10 px-3 py-2 sm:px-5">
            <div className="mx-auto flex max-w-4xl items-center justify-between gap-3">
              <p className="flex min-w-0 items-center gap-2 text-sm font-medium text-foreground">
                <FileCheck2 className="size-4 shrink-0 text-warning" />
                <span className="truncate">
                  {pendingActions.length} pending {pendingActions.length === 1 ? "action" : "actions"} waiting for review
                </span>
              </p>
              <Button type="button" size="sm" variant="outline" onClick={openPendingActions}>
                Review
              </Button>
            </div>
          </div>
        ) : null}
        <WorkspaceComposer thread={selectedThread} value={input} projectLabels={projectLabels} capabilitiesCommands={capabilities.commands} webAvailable={capabilities.web_search_available} webReason={capabilities.web_search_reason} sending={streaming.status === "running"} onChange={setInput} onSubmit={() => void submit()} onCancel={() => abortRef.current?.abort()} onAddProjects={() => { setProjectQuery(""); setProjectPicker("scope"); }} onRemoveProject={(projectId) => { const next = selectedThread.project_ids.filter((item) => item !== projectId); void patchCurrent(next.length ? { scope_mode: "projects", project_ids: next } : { scope_mode: "portfolio", project_ids: [] }); }} onToggleWeb={(enabled) => void patchCurrent({ web_enabled: enabled }).catch((caught) => setError(messageForError(caught)))} />
      </section>
      <Dialog open={mobileHistoryOpen} onOpenChange={setMobileHistoryOpen}>
        <DialogContent showClose={false} className="fixed inset-y-0 left-0 h-dvh max-w-[min(92vw,22rem)] rounded-none p-0">
          <DialogTitle className="sr-only">Conversation history</DialogTitle>
          <ThreadSidebar {...sidebarProps} />
        </DialogContent>
      </Dialog>
      <Dialog open={detailsOpen} onOpenChange={setDetailsOpen}>
        <DialogContent showClose={false} className="fixed inset-y-0 right-0 left-auto h-dvh w-full max-w-[min(92vw,24rem)] rounded-none border-y-0 border-r-0 p-0">
          <DialogTitle className="sr-only">Workspace details</DialogTitle>
          <ContextPanel
            thread={selectedThread}
            projects={projects}
            messages={messages}
            activity={activity}
            liveParts={streaming.parts}
            tab={detailsTab}
            sourceIds={detailsSourceIds}
            onTabChange={setDetailsTab}
            onEditProjects={() => { setProjectQuery(""); setProjectPicker("scope"); }}
            onPortfolio={() => void patchCurrent({ scope_mode: "portfolio", project_ids: [] })}
            onClose={() => setDetailsOpen(false)}
          />
        </DialogContent>
      </Dialog>
      <ResumeDialog open={resumeOpen} initialSearch={resumeQuery} onOpenChange={(open) => { setResumeOpen(open); if (!open) setResumeQuery(""); }} onSelect={selectThread} />
      <ProjectPickerDialog open={projectPicker !== null} initialSearch={projectQuery} mode={projectPicker || "scope"} minimum={projectPicker === "compare" ? 2 : 1} projects={projects} selectedIds={selectedThread.project_ids} onOpenChange={(open) => { if (!open) { setProjectPicker(null); setProjectQuery(""); setPendingCompare(null); } }} onApply={(ids) => void applyProjects(ids)} />
      <HelpDialog open={helpOpen} capabilities={capabilities} onOpenChange={setHelpOpen} />
    </div>
  );
}
