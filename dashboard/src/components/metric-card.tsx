import type { MouseEventHandler } from "react";
import Link from "next/link";
import { ArrowUpRight, CircleAlert, type LucideIcon } from "lucide-react";

import { Card, CardContent } from "@/components/ui/card";
import { cn } from "@/lib/utils";

const toneStyles = {
  neutral: "text-foreground",
  primary: "text-primary",
  success: "text-success",
  warning: "text-warning",
  danger: "text-danger",
};

export function MetricCard({
  label,
  value,
  detail,
  tone = "neutral",
  href,
  icon: Icon = CircleAlert,
  onClick,
}: {
  label: string;
  value: string | number;
  detail: string;
  tone?: keyof typeof toneStyles;
  href?: string | null;
  icon?: LucideIcon;
  onClick?: MouseEventHandler<HTMLAnchorElement>;
}) {
  const content = (
    <Card className="h-full border bg-card py-0 shadow-xs ring-0">
      <CardContent className="flex h-full items-start gap-4 p-4 sm:p-5">
        <span className={cn("mt-0.5 flex size-10 shrink-0 items-center justify-center rounded-xl bg-muted", toneStyles[tone])}>
          <Icon className="size-5" aria-hidden="true" />
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex items-start justify-between gap-3">
            <p className="text-sm font-medium text-muted-foreground">{label}</p>
            {href ? <ArrowUpRight className="size-4 text-muted-foreground" aria-hidden="true" /> : null}
          </div>
          <p className={cn("mt-2 font-mono text-3xl font-semibold tabular-nums", toneStyles[tone])}>{value}</p>
          <p className="mt-1 text-xs leading-5 text-muted-foreground">{detail}</p>
        </div>
      </CardContent>
    </Card>
  );

  return href ? (
    <Link href={href} onClick={onClick} className="block rounded-xl focus-visible:outline-none focus-visible:ring-3 focus-visible:ring-ring/50">
      {content}
    </Link>
  ) : content;
}
