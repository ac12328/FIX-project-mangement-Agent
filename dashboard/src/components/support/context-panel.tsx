"use client";

import { RefreshCw, Send } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import type { BallState, FollowUpInfo } from "@/lib/support-api";

interface ContextPanelProps {
  ball: BallState;
  followup: FollowUpInfo;
  status: string;
  draftText: string;
  onDraftChange: (text: string) => void;
  onRegenerate: () => void;
  onReviewSend: () => void;
  creatingDraft: boolean;
}

export default function ContextPanel({
  ball, followup, status, draftText, onDraftChange, onRegenerate, onReviewSend, creatingDraft,
}: ContextPanelProps) {
  const isWaitingForClient = ball.ball_with === "External";
  const isResolved =
    status === "resolved" ||
    status === "closed" ||
    ball.ball_reason === "status_resolved";

  if (isResolved) {
    return (
      <div className="rounded-xl border bg-card shadow-sm">
        <div className="border-b px-5 py-3 text-sm font-semibold text-foreground">Status</div>
        <div className="px-5 py-8 text-center text-muted-foreground">
          <div className="text-[15px] font-semibold text-foreground">Ticket resolved</div>
          <div className="mt-1 text-[13px] text-muted-foreground">Reopen it if further action is needed.</div>
        </div>
      </div>
    );
  }

  if (isWaitingForClient) {
    return (
      <div className="rounded-xl border bg-card shadow-sm">
        <div className="border-b px-5 py-3 text-sm font-semibold text-foreground">Next action</div>
        <div className="px-5 py-8 text-center text-muted-foreground">
          <div className="text-[15px] font-semibold text-foreground">Waiting for a client reply</div>
          {ball.last_outbound_summary ? (
            <div className="mt-1 text-[13px] text-muted-foreground">
              Last sent: &quot;{ball.last_outbound_summary}&quot;
            </div>
          ) : null}
          {followup.has_followup && followup.due_date ? (
            <div className="mt-4 rounded-lg border border-dashed border-border bg-muted/30 px-3 py-2.5 text-[12px] text-muted-foreground">
              A follow-up draft will generate on <strong>{followup.due_date}</strong> if there is no reply.
            </div>
          ) : null}
        </div>
      </div>
    );
  }

  return (
    <div className="rounded-xl border bg-card shadow-sm">
      <div className="border-b px-5 py-3 text-sm font-semibold text-foreground">Reply draft</div>
      <div className="p-5">
        <Textarea
          className="min-h-[180px] text-sm"
          placeholder="Edit the reply, then review and send."
          value={draftText}
          onChange={(e) => onDraftChange(e.target.value)}
        />
        <div className="mt-3 flex gap-2">
          <Button variant="outline" size="sm" onClick={onRegenerate}>
            <RefreshCw className="size-4" />Regenerate
          </Button>
          <Button size="sm" disabled={!draftText.trim() || creatingDraft} onClick={onReviewSend}>
            <Send className="size-4" />{creatingDraft ? "Creating draft…" : "Review & send"}
          </Button>
        </div>
      </div>
    </div>
  );
}
