"use client";

import { useEffect, useRef, useState } from "react";
import { Check, Copy, RefreshCw, TriangleAlert } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Button } from "@/components/ui/button";
import type { ChatMessage } from "@/lib/chat-types";
import { copyText } from "@/lib/clipboard";

interface AgentChatMessageProps {
  message: ChatMessage;
  onRunAgain?: () => void;
  runAgainDisabled?: boolean;
}

type CopyState = "idle" | "copied" | "error";

export default function AgentChatMessage({
  message,
  onRunAgain,
  runAgainDisabled = false,
}: AgentChatMessageProps) {
  const [copyState, setCopyState] = useState<CopyState>("idle");
  const resetTimerRef = useRef<number | null>(null);

  useEffect(() => () => {
    if (resetTimerRef.current !== null) window.clearTimeout(resetTimerRef.current);
  }, []);

  if (message.role === "user") {
    return (
      <div className="flex min-w-0 justify-end">
        <div className="max-w-[86%] whitespace-pre-wrap break-words rounded-2xl rounded-tr-sm bg-primary px-3 py-2.5 text-sm leading-relaxed text-primary-foreground sm:max-w-[82%]">
          {message.content}
        </div>
      </div>
    );
  }

  const copyMessage = async () => {
    const copied = await copyText(message.content);
    setCopyState(copied ? "copied" : "error");
    if (resetTimerRef.current !== null) window.clearTimeout(resetTimerRef.current);
    resetTimerRef.current = window.setTimeout(() => setCopyState("idle"), 2400);
  };

  return (
    <article className="group min-w-0 overflow-hidden rounded-xl border border-border/70 bg-card px-3.5 py-3 text-card-foreground shadow-sm">
      <div className="prose prose-sm max-w-none min-w-0 overflow-hidden break-words dark:prose-invert prose-headings:mb-2 prose-headings:mt-4 prose-headings:text-sm prose-headings:font-semibold prose-p:my-2 prose-p:min-w-0 prose-p:leading-relaxed prose-li:my-0.5 prose-ul:my-2 prose-ol:my-2 prose-blockquote:my-2 prose-blockquote:border-primary/40 prose-blockquote:text-muted-foreground prose-a:break-all prose-a:text-primary prose-a:underline-offset-2 prose-code:break-all prose-code:rounded prose-code:bg-muted prose-code:px-1 prose-code:py-0.5 prose-code:text-[0.8em] prose-code:before:content-none prose-code:after:content-none prose-pre:my-2 prose-pre:max-w-full prose-pre:overflow-x-auto prose-pre:bg-muted prose-pre:text-xs">
        <ReactMarkdown
          remarkPlugins={[remarkGfm]}
          components={{
            table: ({ children }) => (
              <div className="my-3 max-w-full overflow-x-auto rounded-lg border">
                <table className="m-0 min-w-[360px] text-xs">{children}</table>
              </div>
            ),
          }}
        >
          {message.content}
        </ReactMarkdown>
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-1 border-t border-border/60 pt-2">
        <Button
          type="button"
          variant="ghost"
          size="sm"
          onClick={() => void copyMessage()}
          aria-label={copyState === "copied" ? "Response copied" : "Copy response"}
          className="min-h-11 text-muted-foreground"
        >
          {copyState === "copied" ? <Check /> : copyState === "error" ? <TriangleAlert /> : <Copy />}
          {copyState === "copied" ? "Copied" : copyState === "error" ? "Copy failed" : "Copy"}
        </Button>
        {onRunAgain && (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={onRunAgain}
            disabled={runAgainDisabled}
            aria-label="Run this request again"
            className="min-h-11 text-muted-foreground"
          >
            <RefreshCw />
            Run again
          </Button>
        )}
        {copyState === "error" && (
          <span role="alert" className="basis-full text-xs text-danger">
            Clipboard access failed. Select the response text and copy it manually.
          </span>
        )}
      </div>
    </article>
  );
}
