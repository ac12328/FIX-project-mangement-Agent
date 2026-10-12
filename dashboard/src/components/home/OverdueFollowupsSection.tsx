import type { OverdueFollowup } from "@/lib/types";

interface Props {
  followups: OverdueFollowup[];
  canMarkDone: boolean;
  onMarkDone: (followupId: string) => void;
}

export function OverdueFollowupsSection({ followups, canMarkDone, onMarkDone }: Props) {
  return (
    <section className="overflow-hidden rounded-xl border border-amber-300 dark:border-amber-900/50">
      {/* Header */}
      <div className="flex items-center justify-between border-b border-amber-300 bg-amber-50 px-5 py-3 dark:border-amber-900/50 dark:bg-amber-950/30">
        <div className="flex items-center gap-2">
          <span className="text-xs font-bold uppercase tracking-wider text-amber-700 dark:text-amber-400">
            Overdue Follow-ups
          </span>
          <span className="rounded-full bg-warning px-2 py-0.5 text-xs font-semibold text-white">
            {followups.length}
          </span>
        </div>
        <span className="text-xs text-muted-foreground">Should have been done</span>
      </div>

      {/* Rows */}
      <div className="divide-y divide-amber-100 bg-card dark:divide-amber-900/30">
        {followups.map((f) => (
          <div
            key={f.followup_id}
            className="flex items-center gap-3 px-5 py-2.5"
          >
            <span className="shrink-0 font-mono text-[12px] font-bold text-primary">
              {f.project_id}
            </span>
            <span className="min-w-0 flex-1 truncate text-xs text-muted-foreground">
              {f.description}
            </span>
            <span className="shrink-0 rounded bg-amber-100 px-1.5 py-0.5 text-xs font-semibold text-amber-700 dark:bg-amber-950/50 dark:text-amber-400">
              {f.days_overdue}d overdue
            </span>
            {canMarkDone && (
              <button
                type="button"
                onClick={() => onMarkDone(f.followup_id)}
                className="shrink-0 rounded border border-border px-2.5 py-1 text-xs text-muted-foreground transition-colors hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                Mark done
              </button>
            )}
          </div>
        ))}
      </div>
    </section>
  );
}
