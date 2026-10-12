import Link from "next/link";
import type { ActivityEntry } from "@/lib/types";
import { stripMarkdown } from "@/lib/utils";

const ACTION_COLORS: Record<string, string> = {
  agent_thinking: "bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400",
  state_change: "bg-purple-100 text-purple-700 dark:bg-purple-900/30 dark:text-purple-400",
  email_sent: "bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400",
  email_received: "bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400",
  sla_alert: "bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400",
  error: "bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400",
  go_live_ready_notice: "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-400",
};

const DEFAULT_COLOR =
  "bg-muted text-muted-foreground";

export function ActivityTimeline({
  entries,
  showProject = false,
}: {
  entries: ActivityEntry[];
  showProject?: boolean;
}) {
  if (entries.length === 0) {
    return <p className="text-sm text-muted-foreground">No activity yet.</p>;
  }

  return (
    <div className="space-y-3">
      {entries.map((e) => (
        <div key={e.id} className="flex items-start gap-3">
          <div className="mt-2 h-2 w-2 shrink-0 rounded-full bg-primary" />
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <span
                className={`inline-flex items-center rounded px-1.5 py-0.5 text-xs font-medium ${ACTION_COLORS[e.action_type] ?? DEFAULT_COLOR}`}
              >
                {e.action_type}
              </span>
              <span className="text-xs text-muted-foreground">
                by {e.actor}
              </span>
              {showProject && e.project_id && (
                <Link
                  href={`/projects/${e.project_id}`}
                  className="text-xs font-medium text-primary hover:underline"
                >
                  {e.project_id}
                </Link>
              )}
            </div>
            {e.summary && (
              <p className="mt-1 whitespace-pre-line text-xs text-muted-foreground">
                {stripMarkdown(e.summary)}
              </p>
            )}
            <p className="mt-1 text-xs text-muted-foreground">
              {new Date(e.created_at).toLocaleString()}
            </p>
          </div>
        </div>
      ))}
    </div>
  );
}
