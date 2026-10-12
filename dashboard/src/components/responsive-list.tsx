import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

export function ResponsiveList({
  children,
  className,
  ariaLabel,
}: {
  children: ReactNode;
  className?: string;
  ariaLabel?: string;
}) {
  return (
    <div role="list" aria-label={ariaLabel} className={cn("divide-y overflow-hidden rounded-xl border bg-card", className)}>
      {children}
    </div>
  );
}

export function ResponsiveListItem({
  title,
  metadata,
  status,
  action,
  children,
  className,
}: {
  title: ReactNode;
  metadata?: ReactNode;
  status?: ReactNode;
  action?: ReactNode;
  children?: ReactNode;
  className?: string;
}) {
  return (
    <article role="listitem" className={cn("p-4", className)}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="font-medium text-foreground">{title}</div>
          {metadata ? <div className="mt-1 text-xs text-muted-foreground">{metadata}</div> : null}
        </div>
        {status ? <div className="shrink-0">{status}</div> : null}
      </div>
      {children ? <div className="mt-3 text-sm text-muted-foreground">{children}</div> : null}
      {action ? <div className="mt-4 flex flex-wrap items-center gap-2">{action}</div> : null}
    </article>
  );
}
