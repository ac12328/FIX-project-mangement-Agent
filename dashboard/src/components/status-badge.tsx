import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

const statusColors: Record<string, string> = {
  Active: "border-success/30 bg-success/10 text-success",
  active: "border-success/30 bg-success/10 text-success",
  Blocked: "border-danger/30 bg-danger/10 text-danger",
  blocked: "border-danger/30 bg-danger/10 text-danger",
  critical: "border-danger/30 bg-danger/10 text-danger",
  high: "border-danger/30 bg-danger/10 text-danger",
  Waiting: "border-warning/30 bg-warning/10 text-warning",
  waiting: "border-warning/30 bg-warning/10 text-warning",
  investigating: "border-warning/30 bg-warning/10 text-warning",
  medium: "border-warning/30 bg-warning/10 text-warning",
  "Awaiting Reply": "border-warning/30 bg-warning/10 text-warning",
  Completed: "border-success/30 bg-success/10 text-success",
  completed: "border-success/30 bg-success/10 text-success",
  resolved: "border-success/30 bg-success/10 text-success",
  green: "border-success/30 bg-success/10 text-success",
  Closed: "border-border bg-muted text-muted-foreground",
  closed: "border-border bg-muted text-muted-foreground",
  low: "border-border bg-muted text-muted-foreground",
  yellow: "border-warning/30 bg-warning/10 text-warning",
  red: "border-danger/30 bg-danger/10 text-danger",
  Internal: "border-primary/30 bg-primary/10 text-primary",
  External: "border-warning/30 bg-warning/10 text-warning",
  Client: "border-warning/30 bg-warning/10 text-warning",
  Shared: "border-info/30 bg-info/10 text-info",
  Unknown: "border-border bg-muted text-muted-foreground",
  Running: "border-info/30 bg-info/10 text-info",
  LOW: "border-success/30 bg-success/10 text-success",
  MEDIUM: "border-warning/30 bg-warning/10 text-warning",
  HIGH: "border-danger/30 bg-danger/10 text-danger",
  pending: "border-warning/30 bg-warning/10 text-warning",
  approved: "border-info/30 bg-info/10 text-info",
  queued: "border-info/30 bg-info/10 text-info",
  executing: "border-info/30 bg-info/10 text-info",
  executed: "border-success/30 bg-success/10 text-success",
  uncertain: "border-warning/30 bg-warning/10 text-warning",
  failed: "border-danger/30 bg-danger/10 text-danger",
};

const BALL_WITH_VALUES = new Set(["Internal", "External", "Client", "Shared", "Unknown"]);

export function StatusBadge({
  value,
  label,
  className,
  showDot = true,
}: {
  value: string;
  label?: string;
  className?: string;
  showDot?: boolean;
}) {
  const isBallWith = BALL_WITH_VALUES.has(value);
  return (
    <Badge
      variant="outline"
      className={cn(
        "gap-1.5 rounded-md px-2 py-0.5 text-xs font-medium",
        statusColors[value] ?? "border-border bg-muted text-muted-foreground",
        className,
      )}
    >
      {showDot && !isBallWith ? <span className="inline-block size-1.5 shrink-0 rounded-full bg-current" aria-hidden="true" /> : null}
      {label ?? (isBallWith ? `Ball: ${value}` : value)}
    </Badge>
  );
}
