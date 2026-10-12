"use client";

import Link from "next/link";
import type { ReviewItem, Draft } from "@/lib/types";

interface Props {
  pendingReviewsCount: number;
  pendingDraftsCount: number;
  reviews: ReviewItem[];
  drafts: Draft[];
}

function parseChangeSummary(proposed: string): string {
  try {
    const parsed = JSON.parse(proposed) as Array<{ field?: string; new_value?: string }>;
    if (Array.isArray(parsed) && parsed.length > 0) {
      const first = parsed[0];
      if (first.field && first.new_value) {
        return `${first.field} → ${String(first.new_value).slice(0, 30)}`;
      }
    }
  } catch {
    // ignore parse errors
  }
  return proposed.slice(0, 40);
}

function signalLabel(draft: Draft): string {
  switch (draft.task_type) {
    case "reply_to_client":     return "Reply to inbound";
    case "draft_follow_up":     return "Follow-up";
    case "sla_status_update":   return "SLA deadline";
    default:                    return "Draft";
  }
}

function confidenceClass(confidence: number): string {
  if (confidence >= 0.8) return "bg-success/10 text-success";
  if (confidence >= 0.6) return "bg-warning/10 text-warning";
  return "bg-danger/10 text-danger";
}

export function WorkQueueSection({ pendingReviewsCount, pendingDraftsCount, reviews, drafts }: Props) {
  const total = pendingReviewsCount + pendingDraftsCount;

  return (
    <div className="bg-card border border-primary/20 rounded-xl overflow-hidden">
      {/* Header */}
      <div className="px-4 py-3 bg-primary/5 border-b border-primary/20 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="text-xs font-bold text-primary uppercase tracking-wide">Work Queue</span>
          <span className="text-xs bg-primary text-white rounded-full px-2 py-0.5 font-semibold">
            {pendingReviewsCount > 0 && `${pendingReviewsCount} review${pendingReviewsCount > 1 ? "s" : ""}`}
            {pendingReviewsCount > 0 && pendingDraftsCount > 0 && " · "}
            {pendingDraftsCount > 0 && `${pendingDraftsCount} draft${pendingDraftsCount > 1 ? "s" : ""}`}
            {total === 0 && "clear"}
          </span>
        </div>
        <span className="text-xs text-muted-foreground">Pending your decision</span>
      </div>

      <div className="flex divide-x divide-border">
        {/* Left — Pending Reviews */}
        <div className="flex-[2] p-4">
          <div className="text-xs font-bold uppercase tracking-widest text-primary mb-3">
            Pending Reviews
          </div>
          {reviews.length === 0 ? (
            <p className="text-xs text-muted-foreground">No pending reviews.</p>
          ) : (
            <div className="flex flex-col gap-1.5">
              {reviews.slice(0, 5).map((r) => (
                <div
                  key={r.update_id}
                  className="flex items-center gap-2 px-2.5 py-2 bg-muted/50 rounded-lg"
                >
                  <span className="font-mono font-bold text-primary text-xs">{r.project_id}</span>
                  <span className="text-xs border border-border rounded px-1 text-muted-foreground">
                    {r.task_type}
                  </span>
                  <span className={`text-xs rounded-full px-1.5 font-semibold ${confidenceClass(r.confidence ?? 0)}`}>
                    {((r.confidence ?? 0) * 100).toFixed(0)}%
                  </span>
                  <span className="text-xs text-muted-foreground flex-1 truncate">{parseChangeSummary(r.proposed_changes)}</span>
                  <Link
                    href={`/reviews?id=${r.update_id}`}
                    className="bg-primary text-white rounded px-2 py-1 text-xs font-semibold hover:bg-primary/90"
                  >
                    Review →
                  </Link>
                </div>
              ))}
              {reviews.length > 5 && (
                <Link href="/reviews" className="text-xs text-primary mt-1">
                  +{reviews.length - 5} more →
                </Link>
              )}
            </div>
          )}
        </div>

        {/* Right — Drafts Ready to Send */}
        <div className="flex-1 p-4">
          <div className="text-xs font-bold uppercase tracking-widest text-primary mb-3">
            Drafts to Send
          </div>
          {drafts.length === 0 ? (
            <p className="text-xs text-muted-foreground">No drafts pending.</p>
          ) : (
            <div className="flex flex-col gap-1.5">
              {drafts.slice(0, 6).map((d) => (
                <div
                  key={d.draft_id}
                  className="flex items-center gap-2 px-2.5 py-2 bg-success/5 border border-success/20 rounded-lg"
                >
                  <span className="font-mono font-bold text-primary text-xs">{d.project_id}</span>
                  <span className="text-xs text-muted-foreground bg-muted rounded px-1.5">
                    {signalLabel(d)}
                  </span>
                  <span className="text-xs text-muted-foreground flex-1 truncate">{d.subject}</span>
                  <Link
                    href={`/drafts/${d.draft_id}`}
                    className="bg-success text-white rounded px-2 py-1 text-xs font-semibold hover:bg-success/90"
                  >
                    Send →
                  </Link>
                </div>
              ))}
              {drafts.length > 6 && (
                <Link href="/mail?tab=drafts" className="text-xs text-success mt-1">
                  +{drafts.length - 6} more →
                </Link>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
