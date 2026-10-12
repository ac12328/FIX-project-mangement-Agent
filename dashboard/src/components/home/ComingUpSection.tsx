import type { ComingUpData, ComingUpItem } from "@/lib/types";

interface Props {
  comingUp: ComingUpData;
}

interface ColumnProps {
  label: string;
  dateLabel: string;
  items: ComingUpItem[];
  colorClass: string;
  bgClass: string;
  timeLabel: string;
  timeLabelClass: string;
}

function ComingUpColumn({
  label,
  dateLabel,
  items,
  colorClass,
  bgClass,
  timeLabel,
  timeLabelClass,
}: ColumnProps) {
  return (
    <div className="flex-1 p-4">
      <p className={`mb-2.5 text-xs font-bold uppercase tracking-widest ${colorClass}`}>
        {label} · {dateLabel}
      </p>
      {items.length === 0 ? (
        <p className="text-xs text-muted-foreground">All clear</p>
      ) : (
        <div className="space-y-1.5">
          {items.map((item, idx) => (
            <div
              key={`${item.project_id}-${item.due_date}-${item.label}-${idx}`}
              className={`flex items-center gap-2 rounded-md px-2 py-1.5 ${bgClass}`}
            >
              <span className="font-mono text-xs font-bold text-primary">
                {item.project_id}
              </span>
              <span className="min-w-0 flex-1 truncate text-xs text-foreground">
                {item.label}
              </span>
              <span className={`shrink-0 text-xs font-medium ${timeLabelClass}`}>
                {timeLabel}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function getDayLabel(daysAhead: number): string {
  const d = new Date();
  d.setDate(d.getDate() + daysAhead);
  return d.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" });
}

export function ComingUpSection({ comingUp }: Props) {
  const dayTwoLabel = getDayLabel(2);
  return (
    <section className="overflow-hidden rounded-xl border border-border">
      {/* Header */}
      <div className="flex items-center justify-between border-b border-border bg-muted/50 px-5 py-3">
        <div className="flex items-center gap-2">
          <span className="text-xs font-bold uppercase tracking-wider text-muted-foreground">
            Coming Up
          </span>
          <span className="text-xs text-muted-foreground">Rolling 3 days</span>
        </div>
      </div>

      {/* Three columns */}
      <div className="flex divide-x divide-border bg-card">
        <ComingUpColumn
          label="Today"
          dateLabel={getDayLabel(0)}
          items={comingUp.today}
          colorClass="text-green-600 dark:text-green-400"
          bgClass="bg-green-50 dark:bg-green-950/20"
          timeLabel="due today"
          timeLabelClass="text-green-600 dark:text-green-400"
        />
        <ComingUpColumn
          label="Tomorrow"
          dateLabel={getDayLabel(1)}
          items={comingUp.tomorrow}
          colorClass="text-amber-600 dark:text-amber-400"
          bgClass="bg-amber-50 dark:bg-amber-950/20"
          timeLabel="in 1d"
          timeLabelClass="text-amber-600 dark:text-amber-400"
        />
        <ComingUpColumn
          label={dayTwoLabel.split(",")[0]}
          dateLabel={dayTwoLabel}
          items={comingUp.day2}
          colorClass="text-muted-foreground"
          bgClass="bg-muted/50"
          timeLabel="in 2d"
          timeLabelClass="text-muted-foreground"
        />
      </div>
    </section>
  );
}
