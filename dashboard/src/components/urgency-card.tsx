import Link from "next/link";
import { ArrowRight, FileEdit, Clock, AlertCircle } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import type { BriefItem } from "@/lib/types";

const tierConfig = {
  red: {
    border: "border-l-4 border-l-danger",
    bg: "bg-danger/5",
    scoreBg: "bg-danger text-white",
    dot: "bg-danger",
  },
  yellow: {
    border: "border-l-4 border-l-warning",
    bg: "",
    scoreBg: "bg-warning text-white",
    dot: "bg-warning",
  },
  green: {
    border: "border-l-4 border-l-success",
    bg: "",
    scoreBg: "bg-success text-white",
    dot: "bg-success",
  },
} as const;

export function UrgencyCard({
  item,
  tier,
}: {
  item: BriefItem;
  tier: "red" | "yellow" | "green";
}) {
  const cfg = tierConfig[tier];
  const topReason = item.urgency_reasons[0]
    ? item.urgency_reasons[0].replace(/\s*\(\+\d+\)$/, "")
    : null;

  return (
    <Link href={`/projects/${item.project_id}`} className="group block">
      <Card
        className={cn(
          "border border-border/80 shadow-sm transition-all hover:shadow-md hover:border-primary/30",
          cfg.border,
          cfg.bg,
        )}
      >
        <CardContent className="p-4">
          {/* Row 1: Project ID + ball badge + score */}
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <span className="font-mono text-sm font-semibold tracking-tight">
                  {item.project_id}
                </span>
                {item.ball_with && (
                  <Badge
                    variant="outline"
                    className="text-xs font-medium"
                  >
                    {item.ball_with}
                  </Badge>
                )}
              </div>
              {item.client_name && (
                <p className="mt-0.5 truncate text-xs text-muted-foreground">
                  {item.client_name}
                </p>
              )}
            </div>
            {item.urgency_reasons.length > 0 && tier !== "green" && (
              <span
                className={cn(
                  "inline-flex size-6 shrink-0 items-center justify-center rounded-full text-xs font-bold",
                  cfg.scoreBg,
                )}
                title={`Urgency score: ${item.urgency_reasons.length} signal(s)`}
              >
                {item.urgency_reasons.length}
              </span>
            )}
          </div>

          {/* Row 2: Top reason */}
          {topReason && tier !== "green" && (
            <p className="mt-2 text-xs font-medium text-foreground">
              {topReason}
            </p>
          )}

          {/* Row 3: Milestone + next action */}
          {(item.current_milestone || item.next_action) && (
            <div className="mt-2 space-y-1">
              {item.current_milestone && (
                <p className="text-xs text-muted-foreground">
                  {item.current_milestone}
                </p>
              )}
              {item.next_action && (
                <p className="truncate text-xs text-muted-foreground">
                  → {item.next_action}
                </p>
              )}
            </div>
          )}

          {/* Row 4: Counters + arrow */}
          <div className="mt-3 flex items-center gap-3">
            {item.days_inactive != null && item.days_inactive > 0 && (
              <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
                <Clock className="size-3" />
                {item.days_inactive}d
              </span>
            )}
            {item.pending_drafts > 0 && (
              <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
                <FileEdit className="size-3" />
                {item.pending_drafts}
              </span>
            )}
            {item.open_followups > 0 && (
              <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
                <AlertCircle className="size-3" />
                {item.open_followups}
              </span>
            )}
            <ArrowRight className="ml-auto size-3.5 text-muted-foreground/40 transition-transform group-hover:translate-x-0.5" />
          </div>
        </CardContent>
      </Card>
    </Link>
  );
}
