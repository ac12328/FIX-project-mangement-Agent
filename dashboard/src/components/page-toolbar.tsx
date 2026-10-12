import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

export function PageToolbar({
  children,
  summary,
  className,
}: {
  children: ReactNode;
  summary?: ReactNode;
  className?: string;
}) {
  return (
    <section
      aria-label="Page filters and controls"
      className={cn(
        "flex flex-col gap-3 rounded-xl border bg-card p-3 shadow-xs lg:flex-row lg:items-end lg:justify-between",
        className,
      )}
    >
      <div className="flex min-w-0 flex-1 flex-col gap-3 sm:flex-row sm:flex-wrap sm:items-end">{children}</div>
      {summary ? <div className="shrink-0 text-xs text-muted-foreground">{summary}</div> : null}
    </section>
  );
}

export function ToolbarField({
  label,
  children,
  className,
}: {
  label: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <label className={cn("flex min-w-0 flex-col gap-1.5 text-xs font-medium text-muted-foreground", className)}>
      <span>{label}</span>
      {children}
    </label>
  );
}
