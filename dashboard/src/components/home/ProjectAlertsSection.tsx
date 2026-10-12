import Link from "next/link";
import type { BriefItem } from "@/lib/types";

interface Props {
  items: BriefItem[];
}

export function ProjectAlertsSection({ items }: Props) {
  if (items.length === 0) {
    return (
      <div className="flex items-center gap-3 rounded-xl border border-green-200 bg-green-50 px-5 py-3 dark:border-green-900/50 dark:bg-green-950/30">
        <span className="inline-block size-2 shrink-0 rounded-full bg-green-500" />
        <span className="text-sm font-medium text-green-700 dark:text-green-400">
          All clear — no project alerts.
        </span>
      </div>
    );
  }

  return (
    <section className="overflow-hidden rounded-xl border border-red-200 dark:border-red-900/50">
      {/* Header */}
      <div className="flex items-center justify-between border-b border-red-200 bg-red-50 px-5 py-3 dark:border-red-900/50 dark:bg-red-950/30">
        <div className="flex items-center gap-2">
          <span className="inline-block size-2 rounded-full bg-red-500" />
          <span className="text-xs font-bold uppercase tracking-wider text-red-600 dark:text-red-400">
            Project Alerts
          </span>
          <span className="rounded-full bg-danger px-2 py-0.5 text-xs font-semibold text-white">
            {items.length}
          </span>
        </div>
        <span className="text-xs text-muted-foreground">Needs immediate action</span>
      </div>

      {/* Cards row */}
      <div className="flex flex-wrap divide-x divide-red-100 bg-card dark:divide-red-900/30">
        {items.map((item) => {
          const alertReason =
            item.urgency_reasons.length > 0 ? item.urgency_reasons[0] : item.urgency;
          const clientMilestone = [item.client_name, item.current_milestone]
            .filter(Boolean)
            .join(" · ");

          return (
            <Link
              key={item.project_id}
              href={`/projects/${item.project_id}`}
              className="min-w-[220px] flex-1 p-4 transition-colors hover:bg-red-50/50 dark:hover:bg-red-950/20"
            >
              <div className="mb-1.5 flex flex-wrap items-center gap-2">
                <span className="font-mono text-[13px] font-bold text-primary">
                  {item.project_id}
                </span>
                {alertReason && (
                  <span className="rounded bg-red-100 px-1.5 py-0.5 text-xs font-semibold text-red-600 dark:bg-red-950/50 dark:text-red-400">
                    {alertReason}
                  </span>
                )}
              </div>
              {clientMilestone && (
                <p className="mb-1 text-xs text-muted-foreground">{clientMilestone}</p>
              )}
              {item.next_action && (
                <p className="truncate text-xs text-muted-foreground/80">
                  {item.next_action}
                </p>
              )}
            </Link>
          );
        })}
      </div>
    </section>
  );
}
