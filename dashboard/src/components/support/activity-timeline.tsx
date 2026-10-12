"use client";

import type { ActivityEntry } from "@/lib/support-api";

const dotColorClasses: Record<string, string> = {
  blue: "bg-primary",
  purple: "bg-violet-500",
  amber: "bg-warning",
  gray: "bg-muted-foreground/40",
};

interface ActivityTimelineProps {
  entries: ActivityEntry[];
}

export default function ActivityTimeline({ entries }: ActivityTimelineProps) {
  return (
    <div className="rounded-xl border bg-card shadow-sm">
      <div className="flex items-center justify-between border-b px-5 py-3">
        <span className="text-sm font-semibold text-foreground">Activity Timeline</span>
        <span className="rounded-full bg-muted px-2 py-0.5 text-xs font-semibold text-muted-foreground">{entries.length} events</span>
      </div>
      <div className="divide-y px-5">
        {entries.length === 0 && <p className="py-6 text-center text-sm text-muted-foreground">No activity yet.</p>}
        {entries.map((entry, i) => (
          <div key={i} className="flex items-start gap-3 py-2.5 text-[13px]">
            <span className={`mt-1.5 h-2 w-2 flex-shrink-0 rounded-full ${dotColorClasses[entry.dot_color] ?? dotColorClasses.gray}`} />
            <span className="min-w-[120px] flex-shrink-0 text-xs text-muted-foreground/60">
              {entry.timestamp ? new Date(entry.timestamp).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : ""}
            </span>
            <span className="text-muted-foreground"><strong className="text-foreground">{entry.actor}</strong> {entry.summary}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
