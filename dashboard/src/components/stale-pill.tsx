// dashboard/src/components/stale-pill.tsx
import { cn } from "@/lib/utils";

/**
 * Coloured pill showing days since last activity.
 * Thresholds match AgingDetectionSkill: ≥7d = red, ≥3d = amber, <3d = green.
 * days=0 renders a muted dash (project has no stale data).
 */
export function StalePill({ days }: { days: number }) {
  if (days === 0) {
    return <span className="text-xs text-muted-foreground">—</span>;
  }

  const colorClass =
    days >= 7
      ? "border-danger/30 bg-danger/10 text-danger"
      : days >= 3
        ? "border-warning/30 bg-warning/10 text-warning"
        : "border-success/30 bg-success/10 text-success";

  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-semibold",
        colorClass,
      )}
    >
      {days % 1 === 0 ? `${days}d` : `${days.toFixed(1)}d`}
    </span>
  );
}
