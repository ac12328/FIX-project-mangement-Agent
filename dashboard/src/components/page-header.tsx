"use client";

import type { ReactNode } from "react";

import { useEmbeddedPage } from "@/lib/embedded-page";
import { cn } from "@/lib/utils";

export function PageHeader({
  eyebrow,
  title,
  description,
  metadata,
  actions,
  className,
}: {
  eyebrow?: string;
  title: string;
  description?: string;
  metadata?: ReactNode;
  actions?: ReactNode;
  className?: string;
}) {
  const embedded = useEmbeddedPage();
  if (embedded) {
    if (!metadata && !actions) return null;
    return (
      <div className={cn("flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between", className)}>
        {metadata ? <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">{metadata}</div> : <span />}
        {actions ? <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div> : null}
      </div>
    );
  }
  return (
    <header className={cn("flex flex-col gap-4 border-b pb-5 sm:flex-row sm:items-end sm:justify-between", className)}>
      <div className="min-w-0 space-y-1.5">
        {eyebrow ? <p className="text-xs font-semibold uppercase tracking-[0.16em] text-primary">{eyebrow}</p> : null}
        <h1 className="font-heading text-2xl font-semibold tracking-tight sm:text-3xl">{title}</h1>
        {description ? <p className="max-w-3xl text-sm leading-6 text-muted-foreground">{description}</p> : null}
        {metadata ? <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">{metadata}</div> : null}
      </div>
      {actions ? <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div> : null}
    </header>
  );
}
