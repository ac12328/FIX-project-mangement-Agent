import Link from "next/link";
import type { LucideIcon } from "lucide-react";

import { Button, buttonVariants } from "@/components/ui/button";
import { cn } from "@/lib/utils";

type EmptyStateProps = {
  icon: LucideIcon;
  title: string;
  description: string;
  actionLabel?: string;
  actionHref?: string;
  onAction?: () => void;
  className?: string;
};

export function EmptyState({
  icon: Icon,
  title,
  description,
  actionLabel,
  actionHref,
  onAction,
  className,
}: EmptyStateProps) {
  return (
    <div className={cn("flex flex-col items-center gap-4 rounded-xl border bg-muted/20 p-6 text-center sm:p-8", className)}>
      <div className="flex size-14 items-center justify-center rounded-2xl bg-background text-muted-foreground shadow-xs ring-1 ring-border">
        <Icon className="size-6" aria-hidden="true" />
      </div>
      <div className="space-y-1.5">
        <h2 className="font-heading text-lg font-semibold tracking-tight">{title}</h2>
        <p className="max-w-xl text-sm leading-6 text-muted-foreground">{description}</p>
      </div>
      {actionLabel && (actionHref ? (
        <Link href={actionHref} className={buttonVariants({ variant: "default" })}>{actionLabel}</Link>
      ) : (
        <Button onClick={onAction}>{actionLabel}</Button>
      ))}
    </div>
  );
}
