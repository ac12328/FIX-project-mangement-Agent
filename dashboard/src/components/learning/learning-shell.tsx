"use client";

import type { ReactNode } from "react";
import Link from "next/link";
import { AlertTriangle, Ban, CloudOff, Inbox } from "lucide-react";

import { PageHeader } from "@/components/page-header";
import { LoadingPanel, StatePanel } from "@/components/state-panel";
import { cn } from "@/lib/utils";

const links = [
  ["/learning/candidates", "Candidates"],
  ["/learning/memory", "Memory"],
  ["/learning/conflicts", "Conflicts"],
  ["/learning/jobs", "Jobs"],
  ["/learning/audit", "Audit"],
] as const;

export function LearningShell({
  title,
  description,
  active,
  children,
}: {
  title: string;
  description: string;
  active: string;
  children: ReactNode;
}) {
  return (
    <div className="mx-auto max-w-[1700px] space-y-6">
      <PageHeader
        eyebrow="Learning governance"
        title={title}
        description={description}
        metadata="Audited, revision-bound operational controls"
      />
      <nav
        aria-label="Learning governance sections"
        className="flex max-w-full gap-1 overflow-x-auto rounded-xl border bg-card p-1 shadow-xs"
      >
        {links.map(([href, label]) => (
          <Link
            key={href}
            href={href}
            aria-current={active === href ? "page" : undefined}
            className={cn(
              "inline-flex min-h-11 shrink-0 items-center justify-center rounded-lg px-4 text-sm font-medium outline-none transition-colors focus-visible:ring-3 focus-visible:ring-ring/50",
              active === href
                ? "bg-primary text-primary-foreground shadow-sm"
                : "text-muted-foreground hover:bg-muted hover:text-foreground",
            )}
          >
            {label}
          </Link>
        ))}
      </nav>
      {children}
    </div>
  );
}

export function LearningState({
  state,
  onRetry,
}: {
  state: "loading" | "empty" | "error" | "forbidden" | "stale";
  onRetry?: () => void;
}) {
  if (state === "loading") {
    return <LoadingPanel label="Loading governed learning data" />;
  }

  const copy = {
    empty: {
      icon: Inbox,
      title: "No records",
      description: "There is no governed learning data in this view.",
      tone: "neutral" as const,
    },
    error: {
      icon: AlertTriangle,
      title: "Could not load learning data",
      description: "The Learning Center request failed. Retry to restore the current operational snapshot.",
      tone: "danger" as const,
    },
    forbidden: {
      icon: Ban,
      title: "Access restricted",
      description: "Your operator scope does not permit this governed record.",
      tone: "danger" as const,
    },
    stale: {
      icon: CloudOff,
      title: "Record changed",
      description: "The displayed revision is stale. Refresh before taking another action.",
      tone: "danger" as const,
    },
  };

  const current = copy[state];
  return (
    <StatePanel
      icon={current.icon}
      title={current.title}
      description={current.description}
      tone={current.tone}
      actionLabel={onRetry ? "Refresh" : undefined}
      onAction={onRetry}
    />
  );
}
