"use client";

import { useCallback, useEffect, useState } from "react";
import {
  BarChart3,
  Clock,
  Cpu,
  DollarSign,
  RefreshCw,
  Zap,
} from "lucide-react";

import { EmptyState } from "@/components/empty-state";
import { PageHeader } from "@/components/page-header";
import { PageToolbar } from "@/components/page-toolbar";
import { ErrorPanel } from "@/components/state-panel";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import {
  getLLMAnalyticsByTask,
  getLLMAnalyticsSummary,
  getLLMAnalyticsTrends,
  type LLMDailyTrend as DailyTrend,
  type LLMSummary,
  type LLMTaskBreakdown as TaskBreakdown,
} from "@/lib/api";
import { cn } from "@/lib/utils";

function formatLatency(ms: number): string {
  if (ms >= 1000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.round(ms)}ms`;
}

type Period = 7 | 14 | 30;

export default function LLMAnalyticsPage() {
  const [summary, setSummary] = useState<LLMSummary | null>(null);
  const [tasks, setTasks] = useState<TaskBreakdown[]>([]);
  const [trends, setTrends] = useState<DailyTrend[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [period, setPeriod] = useState<Period>(7);

  const load = useCallback(async () => {
    try {
      setLoading(true);
      const [s, t, tr] = await Promise.all([
        getLLMAnalyticsSummary(period),
        getLLMAnalyticsByTask(period),
        getLLMAnalyticsTrends(period),
      ]);
      setSummary(s);
      setTasks(t.tasks);
      setTrends(tr.days);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load analytics");
    } finally {
      setLoading(false);
    }
  }, [period]);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <div className="mx-auto max-w-[1600px] space-y-6">
      <PageHeader
        eyebrow="AI & Analytics"
        title="LLM analytics"
        description="Monitor usage, latency, token volume, cost, and per-task model behavior."
        metadata={summary ? `${summary.total_calls.toLocaleString()} calls across the last ${summary.period_days} days` : undefined}
        actions={
          <Button variant="outline" disabled={loading} onClick={() => void load()}>
            <RefreshCw className={cn("size-4", loading && "animate-spin")} />
            Refresh
          </Button>
        }
      />

      <PageToolbar summary={`Showing the last ${period} days`}>
        <div className="flex flex-wrap gap-2" role="group" aria-label="Analytics period">
          {([7, 14, 30] as Period[]).map((d) => (
            <Button
              key={d}
              variant={period === d ? "default" : "outline"}
              onClick={() => setPeriod(d)}
              aria-pressed={period === d}
            >
              {d} days
            </Button>
          ))}
        </div>
      </PageToolbar>

      {error ? <ErrorPanel description={error} onRetry={load} /> : null}

      {/* KPI cards */}
      {loading ? (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} className="h-24 w-full" />
          ))}
        </div>
      ) : summary && !error ? (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <KPICard
            icon={Zap}
            label="Total Calls"
            value={summary.total_calls.toLocaleString()}
            sub={`last ${summary.period_days}d`}
          />
          <KPICard
            icon={Cpu}
            label="Total Tokens"
            value={summary.total_tokens.toLocaleString()}
            sub={`${Math.round(summary.total_tokens / Math.max(summary.total_calls, 1))} avg/call`}
          />
          <KPICard
            icon={Clock}
            label="Avg Latency"
            value={formatLatency(summary.avg_latency_ms)}
            sub={summary.avg_latency_ms > 5000 ? "slow" : summary.avg_latency_ms > 2000 ? "moderate" : "fast"}
            tone={summary.avg_latency_ms > 5000 ? "danger" : summary.avg_latency_ms > 2000 ? "warning" : "success"}
          />
          <KPICard
            icon={DollarSign}
            label="Est. Cost"
            value={`$${((summary.total_tokens / 1000) * 0.003).toFixed(2)}`}
            sub={`${summary.error_count} errors (${summary.error_rate_pct}%)`}
            tone={summary.error_rate_pct > 10 ? "danger" : summary.error_rate_pct > 3 ? "warning" : undefined}
          />
        </div>
      ) : null}

      {/* By Task table */}
      {!loading && !error && (
        <Card className="overflow-hidden border border-border/80 bg-card shadow-sm">
          <CardHeader className="pb-2">
            <CardTitle className="text-xs font-medium uppercase tracking-[0.14em] text-muted-foreground">
              By Task
            </CardTitle>
          </CardHeader>
          <CardContent>
            {tasks.length === 0 ? (
              <EmptyState
                icon={BarChart3}
                title="No task data"
                description="LLM task breakdowns will appear here once calls are recorded."
                actionLabel="Refresh"
                onAction={load}
                className="p-5"
              />
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b">
                      <th className="px-3 py-2 text-left text-xs font-medium uppercase tracking-[0.14em] text-muted-foreground">
                        Task
                      </th>
                      <th className="px-3 py-2 text-right text-xs font-medium uppercase tracking-[0.14em] text-muted-foreground">
                        Calls
                      </th>
                      <th className="px-3 py-2 text-right text-xs font-medium uppercase tracking-[0.14em] text-muted-foreground">
                        Avg Tokens
                      </th>
                      <th className="px-3 py-2 text-right text-xs font-medium uppercase tracking-[0.14em] text-muted-foreground">
                        Avg Latency
                      </th>
                      <th className="px-3 py-2 text-right text-xs font-medium uppercase tracking-[0.14em] text-muted-foreground">
                        Errors
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {tasks.map((t) => (
                      <tr
                        key={t.task}
                        className="border-b border-border/50 transition-colors hover:bg-muted/30"
                      >
                        <td className="px-3 py-2">
                          <span className={cn("inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium", taskBadgeClass(t.task))}>
                            {t.task}
                          </span>
                        </td>
                        <td className="px-3 py-2 text-right font-mono text-xs">
                          {t.count}
                        </td>
                        <td className="px-3 py-2 text-right font-mono text-xs">
                          {t.avg_tokens.toLocaleString()}
                        </td>
                        <td className="px-3 py-2 text-right font-mono text-xs">
                          <span
                            className={cn(
                              t.avg_latency_ms > 5000
                                ? "text-danger"
                                : t.avg_latency_ms > 2000
                                  ? "text-warning"
                                  : "text-muted-foreground",
                            )}
                          >
                            {formatLatency(t.avg_latency_ms)}
                          </span>
                        </td>
                        <td className="px-3 py-2 text-right font-mono text-xs">
                          <span className={t.error_count > 0 ? "text-danger" : "text-muted-foreground"}>
                            {t.error_count}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </CardContent>
        </Card>
      )}

      {/* Daily Trends */}
      {!loading && !error && trends.length > 0 && (
        <Card className="overflow-hidden border border-border/80 bg-card shadow-sm">
          <CardHeader className="pb-2">
            <CardTitle className="text-xs font-medium uppercase tracking-[0.14em] text-muted-foreground">
              Daily Trends
            </CardTitle>
          </CardHeader>
          <CardContent>
            {/* Bar sparkline */}
            <div className="mb-4 flex items-end gap-1" style={{ height: 64 }}>
              {trends.map((d) => {
                const maxCalls = Math.max(...trends.map((t) => t.calls), 1);
                const h = Math.max((d.calls / maxCalls) * 100, 4);
                return (
                  <div
                    key={d.date}
                    className={cn(
                      "flex-1 rounded-t transition-colors",
                      d.errors > 0 ? "bg-danger/60" : "bg-primary/40",
                    )}
                    style={{ height: `${h}%` }}
                    title={`${d.date}: ${d.calls} calls, ${d.errors} errors`}
                  />
                );
              })}
            </div>

            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b">
                    <th className="px-3 py-2 text-left text-xs font-medium uppercase tracking-[0.14em] text-muted-foreground">
                      Date
                    </th>
                    <th className="px-3 py-2 text-right text-xs font-medium uppercase tracking-[0.14em] text-muted-foreground">
                      Calls
                    </th>
                    <th className="px-3 py-2 text-right text-xs font-medium uppercase tracking-[0.14em] text-muted-foreground">
                      Tokens
                    </th>
                    <th className="px-3 py-2 text-right text-xs font-medium uppercase tracking-[0.14em] text-muted-foreground">
                      Errors
                    </th>
                    <th className="px-3 py-2 text-right text-xs font-medium uppercase tracking-[0.14em] text-muted-foreground">
                      Avg Latency
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {trends.map((d) => (
                    <tr
                      key={d.date}
                      className="border-b border-border/50 transition-colors hover:bg-muted/30"
                    >
                      <td className="px-3 py-2 font-medium">{d.date}</td>
                      <td className="px-3 py-2 text-right font-mono text-xs">{d.calls}</td>
                      <td className="px-3 py-2 text-right font-mono text-xs">
                        {d.tokens.toLocaleString()}
                      </td>
                      <td className="px-3 py-2 text-right font-mono text-xs">
                        <span className={d.errors > 0 ? "text-danger" : "text-muted-foreground"}>
                          {d.errors}
                        </span>
                      </td>
                      <td className="px-3 py-2 text-right font-mono text-xs">
                        <span
                          className={cn(
                            d.avg_latency_ms > 5000
                              ? "text-danger"
                              : d.avg_latency_ms > 2000
                                ? "text-warning"
                                : "text-muted-foreground",
                          )}
                        >
                          {formatLatency(d.avg_latency_ms)}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  );
}

/* ── Sub-components ──────────────────────────────────────────── */

const TASK_COLORS: Record<string, string> = {
  email_draft: "border-primary/30 bg-primary/10 text-primary",
  triage: "border-warning/30 bg-warning/10 text-warning",
  reply_analysis: "border-info/30 bg-info/10 text-info",
  status_answer: "border-success/30 bg-success/10 text-success",
  query: "border-muted bg-muted text-muted-foreground",
};
function taskBadgeClass(task: string): string {
  for (const [key, cls] of Object.entries(TASK_COLORS)) {
    if (task.includes(key)) return cls;
  }
  return "border-border bg-muted/50 text-muted-foreground";
}

function KPICard({
  icon: Icon,
  label,
  value,
  sub,
  tone,
}: {
  icon: React.ComponentType<{ className?: string }>;
  label: string;
  value: string;
  sub?: string;
  tone?: "success" | "warning" | "danger";
}) {
  const toneClass =
    tone === "danger"
      ? "text-danger"
      : tone === "warning"
        ? "text-warning"
        : tone === "success"
          ? "text-success"
          : "text-muted-foreground";

  return (
    <Card className="border border-border/80 bg-card shadow-sm">
      <CardContent className="flex items-start gap-3 p-4">
        <div className="flex size-10 shrink-0 items-center justify-center rounded-lg bg-muted/50">
          <Icon className="size-4 text-muted-foreground" />
        </div>
        <div className="min-w-0 flex-1">
          <p className="text-xs font-medium uppercase tracking-[0.14em] text-muted-foreground">
            {label}
          </p>
          <p className="mt-0.5 text-xl font-semibold tracking-tight">{value}</p>
          {sub && (
            <p className={cn("mt-0.5 text-xs", toneClass)}>{sub}</p>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
