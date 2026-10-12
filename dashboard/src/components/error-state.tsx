import { AlertTriangle } from "lucide-react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

type ErrorStateProps = {
  title?: string;
  description: string;
  actionLabel?: string;
  onAction?: () => void;
  className?: string;
};

export function ErrorState({
  title = "Could not load data",
  description,
  actionLabel = "Retry",
  onAction,
  className,
}: ErrorStateProps) {
  return (
    <div
      className={cn(
        "flex flex-col items-start gap-4 rounded-2xl border border-destructive/20 bg-destructive/5 p-6 sm:p-8",
        className
      )}
    >
      <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-background text-destructive shadow-sm ring-1 ring-destructive/20">
        <AlertTriangle className="size-5" />
      </div>
      <div className="space-y-2">
        <h2 className="text-lg font-semibold tracking-tight">{title}</h2>
        <p className="max-w-xl text-sm leading-6 text-muted-foreground">
          {description}
        </p>
      </div>
      {onAction ? <Button onClick={onAction}>{actionLabel}</Button> : null}
    </div>
  );
}
