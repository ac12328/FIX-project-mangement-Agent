"use client";

import { KeyboardEvent, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { Bot, ExternalLink, Loader2, Send, Sparkles, Square, X } from "lucide-react";

import { MessageFeed } from "@/components/ai-workspace/message-feed";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";
import {
  createAssistantThread,
  getAssistantCapabilities,
  getAssistantThread,
  listAssistantMessages,
  listAssistantThreads,
  streamAssistantTurn,
} from "@/lib/assistant-api";
import type { AssistantCapabilities, AssistantMessage, AssistantThread } from "@/lib/assistant-types";
import { buildTurnPayload, EMPTY_STREAMING_TURN, reduceStreamingTurn, type StreamingTurnState } from "@/lib/assistant-workspace";
import { useSidebar } from "@/lib/sidebar-context";
import { cn } from "@/lib/utils";

const STARTERS = [
  "Summarize this project.",
  "What should we do next?",
  "What is blocking progress?",
  "/draft Draft the next client update.",
] as const;

export function ProjectAssistantDrawer({ projectId }: { projectId: string }) {
  const { isOpen: sidebarOpen } = useSidebar();
  const [open, setOpen] = useState(false);
  const [thread, setThread] = useState<AssistantThread | null>(null);
  const [messages, setMessages] = useState<AssistantMessage[]>([]);
  const [capabilities, setCapabilities] = useState<AssistantCapabilities | null>(null);
  const [streaming, setStreaming] = useState<StreamingTurnState>(EMPTY_STREAMING_TURN);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => () => abortRef.current?.abort(), []);

  async function ensureThread() {
    if (thread) return thread;
    setLoading(true);
    setError(null);
    try {
      const [page, capabilityResult] = await Promise.all([
        listAssistantThreads({ projectId, limit: 30 }),
        getAssistantCapabilities(),
      ]);
      let active = page.items.find((item) =>
        item.scope_mode === "projects" && item.project_ids.length === 1 && item.project_ids[0] === projectId,
      );
      if (!active) active = await createAssistantThread({ scope_mode: "projects", project_ids: [projectId] });
      const history = await listAssistantMessages(active.thread_id, { limit: 80 });
      setCapabilities(capabilityResult);
      setThread(active);
      setMessages(history.items);
      return active;
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Project AI could not be opened.");
      return null;
    } finally {
      setLoading(false);
    }
  }

  function handleOpenChange(next: boolean) {
    setOpen(next);
    if (next) void ensureThread();
    else abortRef.current?.abort();
  }

  async function submit() {
    const text = input.trim();
    if (!text || streaming.status === "running") return;
    const active = await ensureThread();
    if (!active) return;
    setInput("");
    setError(null);
    const payload = buildTurnPayload(text, active);
    setMessages((current) => [...current, {
      message_id: `local-${payload.client_turn_id}`,
      thread_id: active.thread_id,
      role: "user",
      content: text,
      parts: [{ type: "text", text }],
      created_at: new Date().toISOString(),
    }]);
    setStreaming({ ...EMPTY_STREAMING_TURN, status: "running", progress: "Sending request…" });
    const controller = new AbortController();
    abortRef.current = controller;
    let serverTurnId: string | null = null;
    try {
      await streamAssistantTurn(active.thread_id, payload, {
        signal: controller.signal,
        onEvent: (event) => {
          serverTurnId = event.data.turn_id;
          setStreaming((current) => reduceStreamingTurn(current, event));
        },
        onReconnect: () => setStreaming({
          ...EMPTY_STREAMING_TURN,
          status: "running",
          progress: "Reconnecting to the running request?",
        }),
      });
      const [history, updated] = await Promise.all([
        listAssistantMessages(active.thread_id, { limit: 80 }),
        getAssistantThread(active.thread_id),
      ]);
      setMessages(history.items);
      setThread(updated);
      setStreaming(EMPTY_STREAMING_TURN);
    } catch (caught) {
      const stopped = caught instanceof DOMException && caught.name === "AbortError";
      try {
        const [history, updated] = await Promise.all([
          listAssistantMessages(active.thread_id, { limit: 80 }),
          getAssistantThread(active.thread_id),
        ]);
        setMessages(history.items);
        setThread(updated);
        const terminalSaved = Boolean(serverTurnId && history.items.some(
          (item) => item.turn_id === serverTurnId && item.role === "assistant",
        ));
        if (terminalSaved) {
          setStreaming(EMPTY_STREAMING_TURN);
        } else {
          const message = stopped
            ? "Stopped following this request. It may continue in the background; reopen the thread to recover the result."
            : "Connection lost. The request may still be running in the background; reopen the thread to recover the result.";
          setStreaming((current) => ({ ...current, status: "failed", progress: null, error: message }));
          setError(message);
        }
      } catch {
        const message = stopped
          ? "Stopped following this request. It may continue in the background."
          : caught instanceof Error ? caught.message : "The request failed.";
        setStreaming((current) => ({ ...current, status: "failed", progress: null, error: message }));
        setError(message);
      }
    } finally {
      abortRef.current = null;
    }
  }

  function keyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void submit();
    }
  }

  return (
    <>
      <button
        type="button"
        onClick={() => handleOpenChange(true)}
        aria-label="Ask AI about this project"
        className={cn(
          "fixed bottom-6 right-6 z-40 flex min-h-11 items-center gap-2 rounded-full bg-primary px-4 text-sm font-semibold text-primary-foreground shadow-lg transition-[right,transform,box-shadow] hover:-translate-y-0.5 hover:shadow-xl focus-visible:outline-none focus-visible:ring-3 focus-visible:ring-ring/50",
          sidebarOpen && "lg:right-[23.5rem]",
        )}
      >
        <Sparkles className="size-4" />
        Ask AI
      </button>
      <Dialog open={open} onOpenChange={handleOpenChange}>
        <DialogContent showClose={false} className="fixed inset-y-0 right-0 flex h-dvh w-full max-w-xl flex-col rounded-none p-0 sm:w-[min(92vw,36rem)]">
          <header className="flex min-h-16 items-center gap-3 border-b px-4">
            <span className="flex size-10 items-center justify-center rounded-xl bg-primary/10 text-primary"><Bot className="size-5" /></span>
            <div className="min-w-0 flex-1">
              <DialogTitle className="truncate">Project AI</DialogTitle>
              <DialogDescription className="truncate">Shared AI Workspace thread · read-aware context</DialogDescription>
            </div>
            {thread ? <Button render={<Link href={`/ask?thread=${encodeURIComponent(thread.thread_id)}`} />} size="sm" variant="outline">Open workspace<ExternalLink /></Button> : null}
            <Button type="button" size="icon-sm" variant="ghost" aria-label="Close Project AI" onClick={() => handleOpenChange(false)}><X /></Button>
          </header>
          {error ? <div role="alert" className="border-b border-destructive/25 bg-destructive/5 px-4 py-2 text-sm text-destructive">{error}</div> : null}
          <div className="min-h-0 flex-1 overflow-y-auto">
            <MessageFeed
              messages={messages}
              streaming={streaming}
              loading={loading}
              compact
              canManageActions={Boolean(capabilities?.can_create_actions)}
              operatorWrites={Boolean(capabilities?.operator_writes)}
              starterPrompts={STARTERS}
              onStarter={setInput}
              onResolved={() => {
                if (thread) void listAssistantMessages(thread.thread_id, { limit: 80 }).then((page) => setMessages(page.items));
              }}
            />
          </div>
          <div className="border-t bg-background p-3 pb-[max(0.75rem,env(safe-area-inset-bottom))]">
            <div className="rounded-xl border bg-card p-2 focus-within:border-primary/40 focus-within:ring-3 focus-within:ring-ring/15">
              <label htmlFor={`project-ai-${projectId}`} className="sr-only">Ask about this project</label>
              <Textarea id={`project-ai-${projectId}`} value={input} disabled={streaming.status === "running"} onChange={(event) => setInput(event.target.value)} onKeyDown={keyDown} placeholder="Ask about this project" className="min-h-20 resize-none border-0 shadow-none focus-visible:ring-0" />
              <div className="flex justify-end border-t pt-2">
                {streaming.status === "running" ? <Button type="button" size="sm" variant="outline" onClick={() => abortRef.current?.abort()}><Square className="fill-current" />Stop</Button> : <Button type="button" size="sm" disabled={!input.trim()} onClick={() => void submit()}>{loading ? <Loader2 className="animate-spin" /> : <Send />}Send</Button>}
              </div>
            </div>
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}
