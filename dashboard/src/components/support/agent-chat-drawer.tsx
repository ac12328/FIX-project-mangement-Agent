"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowUp, Bot, Info, RefreshCw, ShieldCheck, X } from "lucide-react";
import { createPortal } from "react-dom";
import { ProjectAssistantDrawer } from "@/components/ai-workspace/project-assistant-drawer";
import AgentChatMessage from "@/components/support/agent-chat-message";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { ApiError, getProjectChatHistory, sendProjectChatMessage } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import type {
  ChatCapabilities,
  ChatHistoryResponse,
  ChatMessage,
  ChatSendResponse,
} from "@/lib/chat-types";
import { useSidebar } from "@/lib/sidebar-context";
import {
  getChatHistory as getSupportChatHistory,
  sendChatMessage as sendSupportChatMessage,
} from "@/lib/support-api";

const historyCache = new Map<string, ChatHistoryResponse>();

async function loadHistory(
  resourceType: "project" | "support",
  resourceId: string,
): Promise<ChatHistoryResponse> {
  return resourceType === "project"
    ? getProjectChatHistory(resourceId)
    : getSupportChatHistory(resourceId);
}

async function sendMessage(
  resourceType: "project" | "support",
  resourceId: string,
  text: string,
  sessionMessages: ChatMessage[],
): Promise<ChatSendResponse> {
  return resourceType === "project"
    ? sendProjectChatMessage(resourceId, text, sessionMessages)
    : sendSupportChatMessage(resourceId, text, sessionMessages);
}

const STARTER_PROMPTS = {
  support: [
    "Summarize this support ticket.",
    "What should we do next?",
    "Draft a reply to the customer.",
    "Find the related project.",
  ],
  project: [
    "Summarize this project.",
    "What should we do next?",
    "What is blocking progress?",
    "Draft the next client update.",
  ],
} as const;

interface AgentChatDrawerProps {
  resourceType: "project" | "support";
  resourceId: string;
}

function cacheKey(
  actorScope: string,
  resourceType: string,
  resourceId: string,
): string {
  return `${actorScope}:${resourceType}:${resourceId}`;
}

function localId(prefix: string): string {
  const value = globalThis.crypto?.randomUUID?.()
    ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `${prefix}-${value}`;
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 404) {
      return "This item is no longer available. Refresh the page and try again.";
    }
    if (error.status === 400 || error.status === 422) {
      return `The request was not accepted: ${error.message}`;
    }
    if (error.status === 401 || error.status === 403) {
      return "Your session does not allow this request. Sign in again or contact an administrator.";
    }
    if (error.status !== null && error.status >= 500) {
      return "The agent service could not complete this request. No confirmed action was reported.";
    }
  }

  const message = error instanceof Error ? error.message : "";
  const normalized = message.toLowerCase();
  if (normalized.includes("timed out") || normalized.includes("timeout")) {
    return "The request timed out while the agent was working. Refresh saved history before sending it again.";
  }
  if (message) return `The request could not be completed: ${message}`;
  return "The request could not be completed. Check the connection and try again.";
}

function scrollBehavior(): ScrollBehavior {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth";
}

export default function AgentChatDrawer(props: AgentChatDrawerProps) {
  if (props.resourceType === "project") {
    return <ProjectAssistantDrawer projectId={props.resourceId} />;
  }
  return <LegacyAgentChatDrawer {...props} />;
}

function LegacyAgentChatDrawer({ resourceType, resourceId }: AgentChatDrawerProps) {
  const { session } = useAuth();
  const { isOpen: sidebarOpen } = useSidebar();
  const actorScope = [
    session?.session_id ?? "no-session",
    session?.user_id ?? session?.operator_id ?? "anonymous",
    session?.role ?? "unknown-role",
    [...(session?.permissions ?? [])].sort().join(","),
  ].join(":");
  const key = cacheKey(actorScope, resourceType, resourceId);
  const cached = historyCache.get(key);
  const [open, setOpen] = useState(false);
  const [portalNode, setPortalNode] = useState<HTMLElement | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>(cached?.messages ?? []);
  const [capabilities, setCapabilities] = useState<ChatCapabilities | null>(
    cached?.capabilities ?? null,
  );
  const [historyLoaded, setHistoryLoaded] = useState(Boolean(cached));
  const [input, setInput] = useState("");
  const [historyLoading, setHistoryLoading] = useState(false);
  const [loading, setLoading] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const [lastSubmitted, setLastSubmitted] = useState<string | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fabRef = useRef<HTMLButtonElement>(null);
  const returnFocusRef = useRef(false);

  const updateConversation = useCallback((data: ChatHistoryResponse) => {
    setMessages(data.messages);
    setCapabilities(data.capabilities);
    setHistoryLoaded(true);
    historyCache.set(key, data);
  }, [key]);

  const refreshHistory = useCallback(async () => {
    setHistoryLoading(true);
    try {
      const data = await loadHistory(resourceType, resourceId);
      updateConversation(data);
      setFailure(null);
    } catch (error) {
      setFailure(errorMessage(error));
    } finally {
      setHistoryLoading(false);
    }
  }, [resourceType, resourceId, updateConversation]);

  const closeDrawer = useCallback((returnFocus = true) => {
    returnFocusRef.current = returnFocus;
    setOpen(false);
  }, []);

  const openDrawer = useCallback(() => {
    returnFocusRef.current = false;
    setOpen(true);
    if (!historyLoaded && !historyLoading) void refreshHistory();
  }, [historyLoaded, historyLoading, refreshHistory]);

  useEffect(() => {
    setPortalNode(document.body);
  }, []);

  useEffect(() => {
    const next = historyCache.get(key);
    setMessages(next?.messages ?? []);
    setCapabilities(next?.capabilities ?? null);
    setHistoryLoaded(Boolean(next));
    setInput("");
    setFailure(null);
    setLastSubmitted(null);
    setOpen(false);
  }, [key]);

  useEffect(() => {
    if (!sidebarOpen) return;
    returnFocusRef.current = false;
    setOpen(false);
  }, [sidebarOpen]);

  useEffect(() => {
    if (open) {
      const frame = window.requestAnimationFrame(() => textareaRef.current?.focus());
      return () => window.cancelAnimationFrame(frame);
    }
    if (returnFocusRef.current && !sidebarOpen) {
      returnFocusRef.current = false;
      const frame = window.requestAnimationFrame(() => fabRef.current?.focus());
      return () => window.cancelAnimationFrame(frame);
    }
  }, [open, sidebarOpen]);

  useEffect(() => {
    if (!open) return;
    const handleEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        closeDrawer(true);
      }
    };
    document.addEventListener("keydown", handleEscape);
    return () => document.removeEventListener("keydown", handleEscape);
  }, [open, closeDrawer]);

  useEffect(() => {
    if (!open) return;
    bottomRef.current?.scrollIntoView({ behavior: scrollBehavior(), block: "nearest" });
  }, [messages, open, loading, failure]);

  const submit = useCallback(async (rawText: string) => {
    const text = rawText.trim();
    if (!text || loading) return;

    const sessionMessages = capabilities?.accepts_session_messages
      ? messages
          .filter((message) => !message.persisted)
          .slice(-capabilities.max_session_messages)
      : [];
    const optimisticTurnId = localId("local-turn");
    const optimisticMessage: ChatMessage = {
      message_id: localId("local-message"),
      turn_id: optimisticTurnId,
      role: "user",
      content: text,
      persisted: false,
    };

    setInput("");
    setFailure(null);
    setLastSubmitted(text);
    setMessages((previous) => [...previous, optimisticMessage]);
    setLoading(true);
    try {
      const data = await sendMessage(resourceType, resourceId, text, sessionMessages);
      updateConversation(data);
    } catch (error) {
      setFailure(errorMessage(error));
    } finally {
      setLoading(false);
    }
  }, [capabilities, loading, messages, resourceType, resourceId, updateConversation]);

  const send = useCallback(() => {
    void submit(input);
  }, [input, submit]);

  const handleKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      send();
    }
  };

  const loadingLabel = resourceType === "support"
    ? "Reviewing ticket context…"
    : "Reviewing project context…";
  const readOnly = capabilities?.read_only_execution ?? false;

  if (sidebarOpen || !portalNode) return null;

  return createPortal(
    (
    <>
      {!open && (
        <button
          ref={fabRef}
          type="button"
          onClick={openDrawer}
          aria-label="Open agent chat"
          aria-expanded="false"
          className="fixed bottom-[max(1rem,env(safe-area-inset-bottom))] right-4 z-50 flex min-h-11 min-w-11 items-center justify-center gap-2 rounded-full bg-primary px-4 py-2.5 text-sm font-semibold text-primary-foreground shadow-lg transition-colors hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-3 focus-visible:ring-ring/50 sm:bottom-[max(1.5rem,env(safe-area-inset-bottom))] sm:right-6"
        >
          <Bot className="size-4" aria-hidden="true" />
          Ask Agent
        </button>
      )}

      {open && (
        <section
          aria-label="Agent chat"
          className="fixed inset-x-3 bottom-[max(0.75rem,env(safe-area-inset-bottom))] top-[max(0.75rem,env(safe-area-inset-top))] z-50 flex min-h-0 min-w-0 flex-col overflow-hidden rounded-2xl border bg-background shadow-2xl sm:inset-auto sm:bottom-[max(1.5rem,env(safe-area-inset-bottom))] sm:right-6 sm:h-[min(700px,calc(100dvh-3rem-env(safe-area-inset-top)-env(safe-area-inset-bottom)))] sm:max-h-[calc(100dvh-3rem-env(safe-area-inset-top)-env(safe-area-inset-bottom))] sm:w-[420px]"
        >
          <div className="flex min-h-14 shrink-0 items-center justify-between border-b px-3 pl-4">
            <div className="flex min-w-0 items-center gap-2">
              <Bot className="size-4 shrink-0 text-primary" aria-hidden="true" />
              <span className="truncate text-sm font-semibold">Agent</span>
              <span className="inline-flex items-center gap-1.5 rounded-full border border-border bg-muted px-2 py-0.5 text-xs font-semibold text-foreground">
                <span
                  className={`size-1.5 rounded-full ${loading ? "bg-primary animate-pulse motion-reduce:animate-none" : "bg-success"}`}
                  aria-hidden="true"
                />
                {loading ? "working" : "ready"}
              </span>
            </div>
            <button
              type="button"
              onClick={() => closeDrawer(true)}
              aria-label="Close chat"
              className="inline-flex size-11 shrink-0 items-center justify-center rounded-lg text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
            >
              <X className="size-4" aria-hidden="true" />
            </button>
          </div>

          {capabilities && (
            <div className="shrink-0 border-b bg-muted/35 px-3 py-2.5 text-xs text-muted-foreground">
              {readOnly ? (
                <div className="flex gap-2">
                  <ShieldCheck className="mt-0.5 size-4 shrink-0 text-primary" aria-hidden="true" />
                  <p>
                    <span className="font-medium text-foreground">Analysis only.</span>{" "}
                    The agent cannot modify business data. This session is not saved and disappears after refresh or navigation.
                  </p>
                </div>
              ) : (
                <div className="flex gap-2">
                  <Info className="mt-0.5 size-4 shrink-0 text-primary" aria-hidden="true" />
                  <p>Chat history is saved for this item. Any action remains limited by your account permissions.</p>
                </div>
              )}
            </div>
          )}

          <div
            role="log"
            aria-live="polite"
            aria-relevant="additions text"
            aria-busy={loading || historyLoading}
            aria-label="Chat messages"
            className="min-h-0 min-w-0 flex-1 space-y-3 overflow-x-hidden overflow-y-auto overscroll-contain p-3"
          >
            {historyLoading && messages.length === 0 && (
              <div role="status" className="pt-8 text-center text-xs text-muted-foreground">
                Loading conversation…
              </div>
            )}

            {historyLoaded && messages.length === 0 && !failure && (
              <div className="space-y-4 pt-4 text-center sm:pt-6">
                <div>
                  <p className="text-sm font-medium">Ask about this {resourceType}.</p>
                  <p className="mt-1 text-xs text-muted-foreground">
                    The agent can review context and suggest the next step.
                  </p>
                </div>
                <div className="flex flex-wrap justify-center gap-2">
                  {STARTER_PROMPTS[resourceType].map((prompt) => (
                    <Button
                      key={prompt}
                      type="button"
                      variant="outline"
                      size="sm"
                      onClick={() => void submit(prompt)}
                      disabled={loading}
                      className="h-auto min-h-11 whitespace-normal py-2 text-left"
                    >
                      {prompt}
                    </Button>
                  ))}
                </div>
              </div>
            )}

            {messages.map((message) => {
              const sourcePrompt = message.role === "assistant"
                ? messages.find(
                    (candidate) => candidate.turn_id === message.turn_id && candidate.role === "user",
                  )?.content
                : undefined;
              return (
                <AgentChatMessage
                  key={message.message_id}
                  message={message}
                  runAgainDisabled={loading}
                  onRunAgain={sourcePrompt ? () => void submit(sourcePrompt) : undefined}
                />
              );
            })}

            {loading && (
              <div role="status" className="flex items-center gap-2 rounded-xl border border-border/70 bg-card px-3 py-2.5 text-sm text-muted-foreground">
                <Bot className="size-4 animate-pulse text-primary motion-reduce:animate-none" aria-hidden="true" />
                {loadingLabel}
              </div>
            )}

            {failure && (
              <div role="alert" className="rounded-xl border border-danger/30 bg-danger/10 p-3 text-sm">
                <p className="text-danger">{failure}</p>
                <div className="mt-2 flex flex-wrap gap-2">
                  {lastSubmitted && (
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      onClick={() => void submit(lastSubmitted)}
                      disabled={loading}
                      className="min-h-11"
                    >
                      <RefreshCw />
                      Run again
                    </Button>
                  )}
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    onClick={() => void refreshHistory()}
                    disabled={historyLoading}
                    className="min-h-11"
                  >
                    Refresh history
                  </Button>
                </div>
              </div>
            )}
            <div ref={bottomRef} />
          </div>

          <div className="flex shrink-0 items-end gap-2 border-t px-2.5 pb-[max(0.625rem,env(safe-area-inset-bottom))] pt-2.5 sm:pb-2.5">
            <Textarea
              ref={textareaRef}
              rows={1}
              value={input}
              onChange={(event) => setInput(event.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="Ask the agent…"
              className="min-h-11 max-h-28 resize-none text-base sm:text-sm"
              aria-label="Message the agent"
            />
            <Button
              size="icon"
              disabled={!input.trim() || loading || historyLoading}
              onClick={send}
              aria-label="Send message"
              className="size-11 shrink-0"
            >
              <ArrowUp className="size-4" aria-hidden="true" />
            </Button>
          </div>
        </section>
      )}
    </>
    ),
    portalNode,
  );
}
