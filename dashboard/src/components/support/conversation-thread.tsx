"use client";

import { useState } from "react";
import type { ConversationMessage } from "@/lib/support-api";

const directionBadge: Record<string, { label: string; className: string }> = {
  inbound: { label: "\u2190 IN", className: "bg-muted/50 text-muted-foreground" },
  outbound: { label: "\u2192 OUT", className: "bg-primary/10 text-primary" },
  internal: { label: "\u2014 NOTE", className: "bg-warning/10 text-warning" },
};

interface ConversationThreadProps {
  messages: ConversationMessage[];
}

export default function ConversationThread({ messages }: ConversationThreadProps) {
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set());

  const toggle = (id: string) => {
    setExpandedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const ordered = [...messages].reverse();

  return (
    <div className="divide-y">
      {ordered.length === 0 && (
        <p className="py-6 text-center text-sm text-muted-foreground">No messages yet.</p>
      )}
      {ordered.map((msg) => {
        const badge = directionBadge[msg.direction] ?? directionBadge.internal;
        const isExpanded = expandedIds.has(msg.evidence_id);
        const isOutbound = msg.direction === "outbound";

        return (
          <div
            key={msg.evidence_id}
            className={`px-4 py-3 ${isOutbound ? "bg-primary/5 border-l-[3px] border-primary" : ""} ${msg.is_latest ? "bg-warning/5" : ""}`}
          >
            <div className="flex items-center gap-2 mb-1">
              <span className={`text-xs font-bold px-1.5 py-0.5 rounded ${badge.className}`}>{badge.label}</span>
              <span className="text-[13px] font-semibold text-foreground">{isOutbound ? (msg.sender_email ?? "Us") : msg.sender_email ?? "Unknown"}</span>
              <span className="ml-auto text-xs text-muted-foreground/60">
                {msg.timestamp ? new Date(msg.timestamp).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : ""}
              </span>
            </div>
            {msg.subject && <p className="text-xs text-muted-foreground mb-1">{msg.subject}</p>}
            {isExpanded && msg.content_text ? (
              <pre className="text-[13px] text-foreground/80 whitespace-pre-wrap leading-relaxed mt-1 max-h-60 overflow-y-auto">{msg.content_text}</pre>
            ) : (
              <p className="text-[13px] text-muted-foreground leading-relaxed">{msg.preview}</p>
            )}
            {msg.content_text && msg.content_text.length > 200 && (
              <button onClick={() => toggle(msg.evidence_id)} className="mt-1 text-xs text-primary hover:underline">
                {isExpanded ? "Collapse \u2191" : "Show full \u2193"}
              </button>
            )}
          </div>
        );
      })}
    </div>
  );
}
