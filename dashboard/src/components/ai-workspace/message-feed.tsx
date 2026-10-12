"use client";

import { useEffect, useRef, useState } from "react";
import {
  Bot,
  Check,
  Copy,
  History,
  Loader2,
  RefreshCw,
  Sparkles,
  TriangleAlert,
  UserRound,
  Files,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { AssistantPartCards } from "@/components/ai-workspace/assistant-cards";
import { CitationChip } from "@/components/ai-workspace/citation-chip";
import { FollowUpCards } from "@/components/ai-workspace/follow-up-rail";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { formatAssistantMessageForCopy } from "@/lib/assistant-copy";
import type { AssistantMessage, AssistantSourcePart } from "@/lib/assistant-types";
import {
  replaceAssistantCitations,
  sanitizeAssistantText,
  sourceIdFromCitationAnchor,
} from "@/lib/assistant-sources";
import type { StreamingTurnState } from "@/lib/assistant-workspace";
import { copyText } from "@/lib/clipboard";
import { cn } from "@/lib/utils";

interface MessageFeedProps {
  messages: AssistantMessage[];
  streaming: StreamingTurnState;
  loading?: boolean;
  loadingEarlier?: boolean;
  hasEarlier?: boolean;
  canManageActions?: boolean;
  operatorWrites?: boolean;
  compact?: boolean;
  starterPrompts?: readonly string[];
  onStarter?: (prompt: string) => void;
  onLoadEarlier?: () => void;
  onActivity?: (message: string) => void;
  onResolved?: () => void;
  onRegenerate?: (message: AssistantMessage) => void;
  onShowSources?: (message: AssistantMessage) => void;
  regenerateDisabled?: boolean;
  followUpQuestions?: readonly string[];
  followUpDisabled?: boolean;
  onFollowUp?: (question: string) => void;
}

function sourceParts(parts: AssistantMessage["parts"]): AssistantSourcePart[] {
  return parts.filter((part): part is AssistantSourcePart => part.type === "source");
}

function MarkdownBody({
  children,
  sources = [],
  compact = false,
}: {
  children: string;
  sources?: AssistantSourcePart[];
  compact?: boolean;
}) {
  const byId = new Map(sources.map((source) => [source.source_id, source]));
  const markdown = replaceAssistantCitations(sanitizeAssistantText(children), sources);
  return (
    <div className={cn(
      "prose max-w-none break-words dark:prose-invert prose-headings:mb-2 prose-headings:mt-5 prose-headings:font-semibold prose-p:my-2.5 prose-p:leading-7 prose-li:my-1 prose-li:leading-7 prose-a:text-primary prose-a:underline-offset-2 prose-code:rounded prose-code:bg-muted prose-code:px-1 prose-code:py-0.5 prose-code:before:content-none prose-code:after:content-none prose-pre:max-w-full prose-pre:overflow-x-auto",
      compact ? "prose-sm" : "prose-base text-[16px]",
    )}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          p: ({ children: paragraph }) => <div className="my-2.5 leading-7">{paragraph}</div>,
          a: ({ href, children: label }) => {
            const sourceId = sourceIdFromCitationAnchor(href);
            if (!sourceId) return <a href={href}>{label}</a>;
            const source = byId.get(sourceId);
            if (!source) return <span>{label}</span>;
            return <CitationChip source={source} />;
          },
        }}
      >
        {markdown}
      </ReactMarkdown>
    </div>
  );
}

type CopyState = "idle" | "copied" | "error";

function AssistantResponseActions({
  message,
  onRegenerate,
  onShowSources,
  regenerateDisabled = false,
}: {
  message: AssistantMessage;
  onRegenerate?: (message: AssistantMessage) => void;
  onShowSources?: (message: AssistantMessage) => void;
  regenerateDisabled?: boolean;
}) {
  const [copyState, setCopyState] = useState<CopyState>("idle");
  const resetTimerRef = useRef<number | null>(null);
  const copyableText = formatAssistantMessageForCopy(message);
  const sources = sourceParts(message.parts);

  useEffect(() => () => {
    if (resetTimerRef.current !== null) window.clearTimeout(resetTimerRef.current);
  }, []);

  const copyMessage = async () => {
    const text = formatAssistantMessageForCopy(message, { origin: window.location.origin });
    const copied = await copyText(text);
    setCopyState(copied ? "copied" : "error");
    if (resetTimerRef.current !== null) window.clearTimeout(resetTimerRef.current);
    resetTimerRef.current = window.setTimeout(() => setCopyState("idle"), 2400);
  };

  if (!copyableText && !onRegenerate && !(sources.length && onShowSources)) return null;

  return (
    <div className="mt-3">
      <div className="flex min-h-11 items-center gap-0.5 text-muted-foreground" aria-label="Response actions">
        {copyableText ? (
          <Tooltip>
            <TooltipTrigger
              render={(
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-sm"
                  onClick={() => void copyMessage()}
                  aria-label={copyState === "copied" ? "Response copied" : copyState === "error" ? "Copy failed" : "Copy response"}
                  className={cn(
                    "size-10",
                    copyState === "copied" && "text-success",
                    copyState === "error" && "text-destructive",
                  )}
                />
              )}
            >
              {copyState === "copied" ? <Check className="size-4" /> : copyState === "error" ? <TriangleAlert className="size-4" /> : <Copy className="size-4" />}
              <span className="sr-only">{copyState === "copied" ? "Copied" : copyState === "error" ? "Copy failed" : "Copy"}</span>
            </TooltipTrigger>
            <TooltipContent>{copyState === "copied" ? "Copied" : copyState === "error" ? "Copy failed" : "Copy"}</TooltipContent>
          </Tooltip>
        ) : null}
        {onRegenerate ? (
          <Tooltip>
            <TooltipTrigger
              render={<Button type="button" variant="ghost" size="icon-sm" className="size-10" disabled={regenerateDisabled} onClick={() => onRegenerate(message)} aria-label="Regenerate response" />}
            >
              <RefreshCw className="size-4" />
            </TooltipTrigger>
            <TooltipContent>Regenerate</TooltipContent>
          </Tooltip>
        ) : null}
        {sources.length && onShowSources ? (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => onShowSources(message)}
            aria-label="Show sources for this response"
            className="px-2.5 text-primary hover:bg-accent hover:text-accent-foreground"
          >
            <Files className="size-4" />
            {sources.length} {sources.length === 1 ? "source" : "sources"}
          </Button>
        ) : null}
      </div>
      {copyState === "error" ? (
        <p role="alert" className="text-xs leading-5 text-destructive">
          Clipboard access failed. Select the response text and copy it manually.
        </p>
      ) : null}
    </div>
  );
}

export function AssistantMessageBubble({
  message,
  canManageActions = false,
  operatorWrites = false,
  compact = false,
  onActivity,
  onResolved,
  onRegenerate,
  onShowSources,
  regenerateDisabled = false,
}: {
  message: AssistantMessage;
  canManageActions?: boolean;
  operatorWrites?: boolean;
  compact?: boolean;
  onActivity?: (message: string) => void;
  onResolved?: () => void;
  onRegenerate?: (message: AssistantMessage) => void;
  onShowSources?: (message: AssistantMessage) => void;
  regenerateDisabled?: boolean;
}) {
  if (message.role === "system") {
    return (
      <div className="flex justify-center py-1">
        <div className="flex max-w-[92%] items-center gap-2 rounded-full bg-muted/60 px-3 py-1.5 text-center text-xs text-muted-foreground">
          <History className="size-3.5 shrink-0" />
          <span>{message.content}</span>
        </div>
      </div>
    );
  }

  if (message.role === "user") {
    return (
      <article className="flex justify-end gap-2">
        <div className={cn(
          "max-w-[90%] whitespace-pre-wrap break-words rounded-2xl rounded-tr-md border border-primary/15 bg-primary/10 px-4 py-2.5 text-[15px] leading-6 text-foreground",
          compact ? "sm:max-w-[88%]" : "sm:max-w-[76%] lg:max-w-[42rem]",
        )}>
          {message.content}
        </div>
        {!compact ? (
          <span className="mt-1 hidden size-7 shrink-0 items-center justify-center rounded-full bg-muted text-muted-foreground sm:flex">
            <UserRound className="size-3.5" />
          </span>
        ) : null}
      </article>
    );
  }

  return (
    <article className="min-w-0">
      <div className="mb-2 flex items-center gap-2 text-xs font-semibold text-foreground/80">
        <span className="flex size-6 items-center justify-center rounded-md bg-primary/10 text-primary">
          <Bot className="size-3.5" />
        </span>
        Agent
      </div>
      <div className="min-w-0">
        {message.parts.some((part) => part.type === "operator_update" && ["confirmed", "cancelled", "expired", "superseded"].includes(String(part.status))) ? (
          <p className="mb-2 max-w-[42rem] text-xs text-muted-foreground">Original proposal - see the action outcome below.</p>
        ) : null}
        {message.content ? (
          <div className={compact ? undefined : "max-w-[42rem]"}>
            <MarkdownBody compact={compact} sources={sourceParts(message.parts)}>{message.content}</MarkdownBody>
          </div>
        ) : null}
        <AssistantPartCards
          parts={message.parts}
          canManageActions={canManageActions}
          operatorWrites={operatorWrites}
          threadId={message.thread_id}
          onActivity={onActivity}
          onResolved={onResolved}
        />
        <div className={compact ? undefined : "max-w-[42rem]"}>
          <AssistantResponseActions
            message={message}
            onRegenerate={onRegenerate}
            onShowSources={onShowSources}
            regenerateDisabled={regenerateDisabled}
          />
          {message.created_at ? (
            <p className="mt-1 text-[11px] text-muted-foreground">
              {new Date(message.created_at).toLocaleString()}
            </p>
          ) : null}
        </div>
      </div>
    </article>
  );
}

function StreamingMessage({
  streaming,
  canManageActions,
  operatorWrites = false,
  compact,
  threadId,
  onActivity,
  onResolved,
}: {
  streaming: StreamingTurnState;
  canManageActions: boolean;
  operatorWrites?: boolean;
  compact: boolean;
  threadId?: string;
  onActivity?: (message: string) => void;
  onResolved?: () => void;
}) {
  if (streaming.status === "idle") return null;
  return (
    <article className="min-w-0" aria-live="polite">
      <div className="mb-2 flex items-center gap-2 text-xs font-semibold text-foreground/80">
        <span className="flex size-6 items-center justify-center rounded-md bg-primary/10 text-primary">
          {streaming.status === "running" ? <Loader2 className="size-3.5 animate-spin" /> : <Bot className="size-3.5" />}
        </span>
        Agent
      </div>
      <div className="min-w-0">
        {streaming.progress ? (
          <div className="mb-3 flex items-center gap-2 text-xs text-muted-foreground">
            <span className="size-1.5 animate-pulse rounded-full bg-primary" />
            {streaming.progress}
          </div>
        ) : null}
        {streaming.text ? (
          <div className={compact ? undefined : "max-w-[42rem]"}>
            <MarkdownBody compact={compact} sources={sourceParts(streaming.parts)}>{streaming.text}</MarkdownBody>
          </div>
        ) : null}
        <AssistantPartCards
          parts={streaming.parts}
          canManageActions={canManageActions}
          operatorWrites={operatorWrites}
          threadId={threadId}
          onActivity={onActivity}
          onResolved={onResolved}
        />
        {streaming.error ? <p role="alert" className="mt-2 text-sm text-destructive">{streaming.error}</p> : null}
      </div>
    </article>
  );
}

export function MessageFeed({
  messages,
  streaming,
  loading = false,
  loadingEarlier = false,
  hasEarlier = false,
  canManageActions = false,
  operatorWrites = false,
  compact = false,
  starterPrompts = [],
  onStarter,
  onLoadEarlier,
  onActivity,
  onResolved,
  onRegenerate,
  onShowSources,
  regenerateDisabled = false,
  followUpQuestions = [],
  followUpDisabled = false,
  onFollowUp,
}: MessageFeedProps) {
  const bottomRef = useRef<HTMLDivElement>(null);
  const lastMessageId = messages.at(-1)?.message_id;

  useEffect(() => {
    if (!loading && lastMessageId) bottomRef.current?.scrollIntoView({ block: "end" });
  }, [lastMessageId, loading]);

  useEffect(() => {
    if (streaming.status === "running") bottomRef.current?.scrollIntoView({ block: "end" });
  }, [streaming.parts.length, streaming.progress, streaming.status, streaming.text]);

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center" role="status">
        <div className="flex items-center gap-3 text-sm text-muted-foreground">
          <Loader2 className="size-4 animate-spin" />
          Loading conversation…
        </div>
      </div>
    );
  }

  return (
    <div className={cn("mx-auto flex w-full flex-col gap-6", compact ? "max-w-3xl px-3 py-4" : "max-w-[56rem] px-4 py-6 sm:px-8 lg:py-9")}>
      {hasEarlier ? (
        <div className="flex justify-center">
          <Button type="button" variant="ghost" size="sm" onClick={onLoadEarlier} disabled={loadingEarlier}>
            {loadingEarlier ? <Loader2 className="animate-spin" /> : <History />}
            Load earlier messages
          </Button>
        </div>
      ) : null}

      {messages.length === 0 && streaming.status === "idle" ? (
        <section className={cn("mx-auto flex max-w-2xl flex-col items-center text-center", compact ? "py-8" : "py-12 sm:py-20")}>
          <span className="flex size-14 items-center justify-center rounded-2xl bg-primary/10 text-primary ring-1 ring-primary/15">
            <Sparkles className="size-6" />
          </span>
          <h2 className="mt-5 text-xl font-semibold">What would you like to understand?</h2>
          <p className="mt-2 max-w-lg text-sm leading-6 text-muted-foreground">
            Ask naturally, use a slash command, compare projects, inspect portfolio status, or create a draft for review.
          </p>
          {starterPrompts.length ? (
            <div className="mt-6 grid w-full gap-2 sm:grid-cols-2">
              {starterPrompts.map((prompt) => (
                <button
                  key={prompt}
                  type="button"
                  onClick={() => onStarter?.(prompt)}
                  className="min-h-11 rounded-xl border bg-card px-4 py-3 text-left text-sm font-medium transition-colors hover:border-primary/30 hover:bg-accent/40 focus-visible:outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
                >
                  {prompt}
                </button>
              ))}
            </div>
          ) : null}
        </section>
      ) : null}

      {messages.map((message) => (
        <AssistantMessageBubble
          key={message.message_id}
          message={message}
          compact={compact}
          canManageActions={canManageActions}
          operatorWrites={operatorWrites}
          onActivity={onActivity}
          onResolved={onResolved}
          onRegenerate={onRegenerate}
          onShowSources={onShowSources}
          regenerateDisabled={regenerateDisabled}
        />
      ))}
      <StreamingMessage
        streaming={streaming}
        compact={compact}
        canManageActions={canManageActions}
        operatorWrites={operatorWrites}
        threadId={messages[0]?.thread_id}
        onActivity={onActivity}
        onResolved={onResolved}
      />
      {streaming.status !== "running" && onFollowUp ? (
        <FollowUpCards
          questions={[...followUpQuestions]}
          disabled={followUpDisabled}
          onQuestion={onFollowUp}
        />
      ) : null}
      <div ref={bottomRef} />
    </div>
  );
}
