import type { LucideIcon } from "lucide-react";
import { AlertTriangle, Inbox, LoaderCircle } from "lucide-react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

const tones = {
  neutral: "border-border bg-muted/30 text-muted-foreground",
  danger: "border-destructive/20 bg-destructive/5 text-destructive",
};

export function StatePanel({
  title,
  description,
  icon: Icon = Inbox,
  actionLabel,
  onAction,
  actionDisabled = false,
  tone = "neutral",
  className,
}: {
  title: string;
  description: string;
  icon?: LucideIcon;
  actionLabel?: string;
  onAction?: () => void;
  actionDisabled?: boolean;
  tone?: keyof typeof tones;
  className?: string;
}) {
  return (
    <div className={cn("flex flex-col items-start gap-3 rounded-xl border p-5", tones[tone], className)}>
      <span className="flex size-10 items-center justify-center rounded-xl bg-background shadow-xs ring-1 ring-current/10">
        <Icon className="size-5" aria-hidden="true" />
      </span>
      <div>
        <h3 className="font-heading text-base font-semibold text-foreground">{title}</h3>
        <p className="mt-1 max-w-2xl text-sm leading-6 text-muted-foreground">{description}</p>
      </div>
      {actionLabel && onAction ? <Button variant="outline" size="sm" onClick={onAction} disabled={actionDisabled}>{actionLabel}</Button> : null}
    </div>
  );
}

export function LoadingPanel({ label = "Loading workspace data" }: { label?: string }) {
  return (
    <div className="flex min-h-40 items-center justify-center rounded-xl border bg-card" role="status">
      <span className="flex items-center gap-2 text-sm text-muted-foreground">
        <LoaderCircle className="size-4 animate-spin" aria-hidden="true" />
        {label}
      </span>
    </div>
  );
}

export function ErrorPanel({ description, onRetry }: { description: string; onRetry?: () => void }) {
  return (
    <StatePanel
      icon={AlertTriangle}
      title="This section could not be loaded"
      description={description}
      tone="danger"
      actionLabel={onRetry ? "Retry" : undefined}
      onAction={onRetry}
    />
  );
}
