"use client";

import { Badge } from "@/components/ui/badge";
import type { BallState, FollowUpInfo } from "@/lib/support-api";

const ballLabels: Record<string, string> = {
  External: "With Client",
  Internal: "With Us",
  Shared: "Shared",
  Unknown: "Unknown",
};

const ballColors: Record<string, string> = {
  External: "border-warning bg-warning/10 text-warning",
  Internal: "border-primary bg-primary/10 text-primary",
  Shared: "border-info bg-info/10 text-info",
  Unknown: "border-border bg-muted/30 text-muted-foreground",
};

const ballDotColors: Record<string, string> = {
  External: "bg-warning",
  Internal: "bg-primary",
  Shared: "bg-info",
  Unknown: "bg-muted-foreground/40",
};

const severityColors: Record<string, string> = {
  critical: "border-danger/30 bg-danger/10 text-danger",
  high: "border-warning/30 bg-warning/10 text-warning",
  medium: "border-border bg-muted/50 text-muted-foreground",
  low: "border-success/30 bg-success/10 text-success",
};

const statusColors: Record<string, string> = {
  open: "border-info/30 bg-info/10 text-info",
  investigating: "border-primary/30 bg-primary/10 text-primary",
  waiting_external: "border-warning/30 bg-warning/10 text-warning",
  waiting_internal: "border-info/30 bg-info/10 text-info",
  resolved: "border-success/30 bg-success/10 text-success",
  closed: "border-border bg-muted/50 text-muted-foreground",
};

const statusLabels: Record<string, string> = {
  open: "Open",
  investigating: "Investigating",
  waiting_external: "Waiting for Reply",
  waiting_internal: "Waiting Internal",
  resolved: "Resolved",
  closed: "Closed",
};

function formatSince(iso: string | null): string {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleDateString(undefined, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return "";
  }
}

interface StatusStripProps {
  ball: BallState;
  status: string;
  severity: string;
  followup: FollowUpInfo;
  onFollowupChange?: () => void;
  onFollowupCancel?: () => void;
}

export default function StatusStrip({
  ball, status, severity, followup, onFollowupChange, onFollowupCancel,
}: StatusStripProps) {
  const since = formatSince(ball.since);

  return (
    <div className="flex items-center gap-4 flex-wrap rounded-xl border bg-card p-4 shadow-sm">
      <div className={`flex items-center gap-2.5 rounded-lg border-2 px-4 py-2.5 text-sm font-semibold ${ballColors[ball.ball_with] ?? ballColors.Unknown}`}>
        <span className={`h-2.5 w-2.5 rounded-full ${ballDotColors[ball.ball_with] ?? ballDotColors.Unknown}`} />
        Ball: {ballLabels[ball.ball_with] ?? "Unknown"}
        {since && <span className="ml-1 text-xs font-normal opacity-70">&mdash; since {since}</span>}
      </div>
      <Badge variant="outline" className={`text-xs font-semibold ${statusColors[status] ?? ""}`}>
        {statusLabels[status] ?? status}
      </Badge>
      <Badge variant="outline" className={`text-xs font-semibold ${severityColors[severity] ?? ""}`}>
        {severity}
      </Badge>
      {followup.has_followup && followup.due_date && (
        <div className="ml-auto flex items-center gap-2 rounded-lg bg-muted/50 px-3 py-2 text-[13px] text-foreground">
          <span>Auto follow-up: <strong>{followup.due_date}</strong></span>
          {onFollowupChange && <button onClick={onFollowupChange} className="text-xs text-primary hover:underline">Change</button>}
          {onFollowupCancel && (
            <><span className="text-border">|</span><button onClick={onFollowupCancel} className="text-xs text-primary hover:underline">Cancel</button></>
          )}
        </div>
      )}
    </div>
  );
}
