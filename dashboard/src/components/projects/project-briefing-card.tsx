"use client";

import {
  ArrowRight,
  CircleAlert,
  CircleCheckBig,
  Clock,
  Mail,
  RefreshCw,
  Sparkles,
  Users,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import type { ProjectSummaryAI } from "@/lib/project-briefing-api";

interface ProjectBriefingCardProps {
  summary: ProjectSummaryAI | null;
  loading: boolean;
  onRegenerate: () => void;
}

function formatBriefingTime(value: string | null | undefined): string {
  if (!value) return "No email evidence";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return `${new Intl.DateTimeFormat("en-GB", {
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: "Asia/Hong_Kong",
  }).format(date)} HKT`;
}

function formatBriefingEventType(value: string): string {
  return value
    .split("_")
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

export function ProjectBriefingCard({
  summary,
  loading,
  onRegenerate,
}: ProjectBriefingCardProps) {
  return (
    <Card className="overflow-hidden border-primary/20 bg-gradient-to-br from-primary/5 via-background to-accent/10">
      <CardHeader className="border-b border-border/70 pb-3 pt-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-2.5">
            <div className="flex size-8 items-center justify-center rounded-lg bg-primary/10">
              <Sparkles className="size-4 text-primary" aria-hidden="true" />
            </div>
            <div>
              <CardTitle className="text-sm font-semibold">Evidence-aware AI Summary</CardTitle>
              <p className="text-xs text-muted-foreground">
                Latest thread evidence takes precedence over earlier proposals.
              </p>
            </div>
          </div>
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="min-h-11 gap-1.5 text-xs"
            onClick={onRegenerate}
            disabled={loading}
          >
            <RefreshCw
              className={`size-3.5 ${loading ? "animate-spin" : ""}`}
              aria-hidden="true"
            />
            {loading ? "Refreshing" : "Regenerate"}
          </Button>
        </div>
      </CardHeader>
      <CardContent className="space-y-5 pt-5" aria-live="polite">
        {loading && !summary ? (
          <div
            className="space-y-4"
            role="status"
            aria-busy="true"
            aria-label="Loading project summary"
          >
            <Skeleton className="h-24 w-full" />
            <Skeleton className="h-40 w-full" />
            <Skeleton className="h-24 w-full" />
          </div>
        ) : summary ? (
          <>
            <section
              aria-labelledby="briefing-current-state"
              className="rounded-xl border bg-background/80 p-4 shadow-sm"
            >
              <div className="mb-2 flex items-center gap-2 text-primary">
                <CircleCheckBig className="size-4" aria-hidden="true" />
                <h3
                  id="briefing-current-state"
                  className="text-xs font-semibold uppercase tracking-wider"
                >
                  Current State
                </h3>
              </div>
              <p className="text-base font-medium leading-7 text-foreground">
                {summary.current_state}
              </p>
            </section>

            <section aria-labelledby="briefing-evidence-chain">
              <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                <div className="flex items-center gap-2">
                  <Mail className="size-4 text-muted-foreground" aria-hidden="true" />
                  <h3
                    id="briefing-evidence-chain"
                    className="text-xs font-semibold uppercase tracking-wider text-muted-foreground"
                  >
                    Evidence Chain
                  </h3>
                </div>
                <Badge variant="secondary" className="font-normal">
                  {summary.source_count} source{summary.source_count === 1 ? "" : "s"}
                </Badge>
              </div>
              {summary.events.length ? (
                <ol className="relative ml-2 space-y-4 border-l border-border pl-5">
                  {summary.events.slice(-5).map((event) => (
                    <li
                      key={`${event.source_ref}-${event.occurred_at}`}
                      className="relative"
                    >
                      <span
                        className="absolute -left-[1.62rem] top-1.5 size-2.5 rounded-full border-2 border-background bg-primary"
                        aria-hidden="true"
                      />
                      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                        <span className="text-sm font-semibold text-foreground">
                          {event.actor_name}
                        </span>
                        <Badge
                          variant="outline"
                          className="h-5 px-1.5 text-xs font-normal capitalize"
                        >
                          {event.actor_role}
                        </Badge>
                        <span className="text-xs text-muted-foreground">
                          {formatBriefingTime(event.occurred_at)}
                        </span>
                      </div>
                      <p className="mt-1 text-sm leading-6 text-foreground">
                        {event.summary}
                      </p>
                      <p className="mt-1 text-xs text-muted-foreground">
                        {formatBriefingEventType(event.event_type)} | {event.source_ref}
                      </p>
                    </li>
                  ))}
                </ol>
              ) : (
                <p className="rounded-lg border border-dashed p-3 text-sm text-muted-foreground">
                  No email evidence is available yet; this summary is based on canonical project state.
                </p>
              )}
            </section>

            <section
              aria-labelledby="briefing-next-action"
              className="rounded-xl border border-primary/30 bg-primary/10 p-4"
            >
              <div className="mb-2 flex items-center gap-2 text-primary">
                <ArrowRight className="size-4" aria-hidden="true" />
                <h3
                  id="briefing-next-action"
                  className="text-xs font-semibold uppercase tracking-wider"
                >
                  Next Action
                </h3>
              </div>
              <p className="text-sm font-semibold leading-6 text-foreground">
                {summary.next_action}
              </p>
            </section>

            {summary.has_blocker ? (
              <section
                aria-labelledby="briefing-blocker"
                className="rounded-lg border border-destructive/30 bg-destructive/5 p-3"
              >
                <div className="flex items-start gap-2">
                  <CircleAlert
                    className="mt-0.5 size-4 shrink-0 text-destructive"
                    aria-hidden="true"
                  />
                  <div>
                    <h3
                      id="briefing-blocker"
                      className="text-xs font-semibold uppercase tracking-wider text-destructive"
                    >
                      Active Blocker
                    </h3>
                    <p className="mt-1 text-sm leading-6 text-foreground">
                      {summary.blocker}
                    </p>
                  </div>
                </div>
              </section>
            ) : (
              <div className="flex items-center gap-2 rounded-lg border bg-muted/30 px-3 py-2 text-sm">
                <CircleCheckBig
                  className="size-4 shrink-0 text-[var(--success)]"
                  aria-hidden="true"
                />
                <span className="font-medium text-foreground">No active blocker</span>
              </div>
            )}

            <footer className="grid gap-3 border-t pt-4 text-xs text-muted-foreground sm:grid-cols-2 xl:grid-cols-4">
              <BriefingFact icon={Users} label="Ball with" value={summary.ball_with} />
              <BriefingFact
                icon={Mail}
                label="Evidence cutoff"
                value={formatBriefingTime(summary.evidence_cutoff_at)}
              />
              <BriefingFact
                icon={Clock}
                label="Generated"
                value={formatBriefingTime(summary.generated_at)}
              />
              <BriefingFact icon={Mail} label="Sources used" value={String(summary.source_count)} />
            </footer>
          </>
        ) : (
          <p className="text-sm text-muted-foreground">
            Summary unavailable. Regenerate to retry.
          </p>
        )}
      </CardContent>
    </Card>
  );
}

interface BriefingFactProps {
  icon: typeof Clock;
  label: string;
  value: string;
}

function BriefingFact({ icon: Icon, label, value }: BriefingFactProps) {
  return (
    <div className="flex items-start gap-2">
      <Icon className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
      <div>
        <span className="block">{label}</span>
        <strong className="font-semibold text-foreground">{value}</strong>
      </div>
    </div>
  );
}
