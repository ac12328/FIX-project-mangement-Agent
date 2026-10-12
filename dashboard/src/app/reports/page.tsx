"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import {
  BarChart3,
  FileText,
  RefreshCw,
  Table2,
} from "lucide-react";
import ReactMarkdown from "react-markdown";

import { EmptyState } from "@/components/empty-state";
import { PageHeader } from "@/components/page-header";
import { PageToolbar } from "@/components/page-toolbar";
import { ErrorPanel, StatePanel } from "@/components/state-panel";
import { StatusBadge } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { getReport } from "@/lib/api";
import type { ReportData } from "@/lib/types";

export default function ReportsPage() {
  const [report, setReport] = useState<ReportData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [type, setType] = useState<"weekly" | "monthly" | "quarterly">("weekly");
  const [view, setView] = useState<"markdown" | "table">("markdown");

  const load = useCallback((t: "weekly" | "monthly" | "quarterly") => {
    if (t === "quarterly") return;
    setLoading(true);
    setError(null);
    getReport(t)
      .then(setReport)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  // eslint-disable-next-line react-hooks/set-state-in-effect -- async data fetch, not a cascading render
  useEffect(() => { load(type); }, [type, load]);

  return (
    <div className="mx-auto max-w-[1600px] space-y-6">
      <PageHeader
        eyebrow="AI & Analytics"
        title="Portfolio reports"
        description="Weekly or monthly snapshot for sharing. Print or save as PDF."
        metadata={report ? `${report.period_start} – ${report.period_end}` : undefined}
        actions={
          <>
            <Button render={<Link href="/reports/projects" />}>Project summary</Button>
            <Button variant="outline" render={<Link href="/operations" />}>
              Back to operations
            </Button>
            <Button variant="outline" onClick={() => window.print()}>
              <FileText className="size-4" />
              Export PDF
            </Button>
            <Button variant="outline" disabled={type === "quarterly" || loading} onClick={() => load(type)}>
              <RefreshCw className="size-4" />
              Refresh
            </Button>
          </>
        }
      />

      <PageToolbar summary={`${type.charAt(0).toUpperCase() + type.slice(1)} report · ${view} view`}>
        <div className="flex flex-wrap gap-2" role="group" aria-label="Report period">
          {(["weekly", "monthly", "quarterly"] as const).map((t) => (
            <Button
              key={t}
              variant={type === t ? "default" : "outline"}
              onClick={() => setType(t)}
              aria-pressed={type === t}
            >
              {t.charAt(0).toUpperCase() + t.slice(1)}
            </Button>
          ))}
        </div>
        <Button
          variant="outline"
          onClick={() => setView((v) => (v === "markdown" ? "table" : "markdown"))}
        >
          {view === "markdown" ? (
            <><Table2 className="size-4" /> Table view</>
          ) : (
            <><FileText className="size-4" /> Markdown view</>
          )}
        </Button>
      </PageToolbar>

      {error ? <ErrorPanel description={error} onRetry={() => load(type)} /> : null}

      {type === "quarterly" ? (
        <StatePanel
          icon={BarChart3}
          title="No quarterly report available"
          description="Quarterly reports are generated on the first day of each quarter."
        />
      ) : loading ? (
        <ReportSkeleton />
      ) : report && !error ? (
        <>
          {/* Stats bar — table view only */}
          {view === "table" && (
            <div className="grid gap-3 sm:grid-cols-3 lg:grid-cols-5">
              {[
                { label: "Active Projects", value: report.snapshots.length },
                { label: "Milestones Advanced", value: Object.values(report.milestone_distribution).reduce((a, b) => a + b, 0) },
                { label: "Highlights", value: report.highlights.length },
                { label: "On Track", value: report.snapshots.filter(s => s.project_status === "Active").length },
                { label: "Blocked", value: report.snapshots.filter(s => s.project_status === "Blocked").length },
              ].map(({ label, value }) => (
                <div key={label} className="rounded-xl border border-border/80 bg-card p-3 text-center shadow-sm">
                  <p className="text-2xl font-bold tracking-tight">{value}</p>
                  <p className="mt-0.5 text-xs font-medium uppercase tracking-[0.12em] text-muted-foreground">{label}</p>
                </div>
              ))}
            </div>
          )}

          {view === "markdown" ? (
            <Card className="border border-border/80 bg-card shadow-sm">
              <CardContent className="max-h-[36rem] overflow-auto p-5">
                <div className="prose prose-sm max-w-none dark:prose-invert prose-headings:tracking-tight prose-h2:text-base prose-h3:text-sm prose-p:leading-relaxed prose-li:leading-relaxed prose-pre:bg-muted/50 prose-pre:text-xs">
                  <ReactMarkdown>{report.markdown}</ReactMarkdown>
                </div>
              </CardContent>
            </Card>
          ) : (
            <div className="space-y-4">
              {/* Highlights */}
              {report.highlights.length > 0 && (
                <Card className="border border-border/80 bg-card shadow-sm">
                  <CardHeader className="pb-2">
                    <CardTitle className="text-xs font-medium uppercase tracking-[0.14em] text-muted-foreground">
                      Highlights
                    </CardTitle>
                  </CardHeader>
                  <CardContent>
                    <ul className="space-y-1.5 text-sm">
                      {report.highlights.map((h, i) => (
                        <li key={i} className="flex items-start gap-2">
                          <span className="mt-1.5 size-1.5 shrink-0 rounded-full bg-primary" />
                          <span>{h}</span>
                        </li>
                      ))}
                    </ul>
                  </CardContent>
                </Card>
              )}

              {/* Milestone distribution */}
              {Object.keys(report.milestone_distribution).length > 0 && (
                <Card className="border border-border/80 bg-card shadow-sm">
                  <CardHeader className="pb-2">
                    <CardTitle className="text-xs font-medium uppercase tracking-[0.14em] text-muted-foreground">
                      Milestone Distribution
                    </CardTitle>
                  </CardHeader>
                  <CardContent>
                    <div className="space-y-2">
                      {Object.entries(report.milestone_distribution).map(([k, v]) => {
                        const total = Object.values(report.milestone_distribution).reduce(
                          (a, b) => a + b,
                          0,
                        );
                        const pct = total > 0 ? (v / total) * 100 : 0;
                        return (
                          <div key={k} className="flex items-center gap-3 text-sm">
                            <span className="w-24 shrink-0 truncate text-xs text-muted-foreground">
                              {k}
                            </span>
                            <div className="flex-1">
                              <div className="h-2 overflow-hidden rounded-full bg-muted/50">
                                <div
                                  className="h-full rounded-full bg-primary/60 transition-all"
                                  style={{ width: `${pct}%` }}
                                />
                              </div>
                            </div>
                            <span className="w-8 text-right font-mono text-xs font-medium">
                              {v}
                            </span>
                          </div>
                        );
                      })}
                    </div>
                  </CardContent>
                </Card>
              )}

              {/* Snapshots table */}
              <Card className="overflow-hidden border border-border/80 bg-card shadow-sm">
                <CardHeader className="pb-2">
                  <CardTitle className="text-xs font-medium uppercase tracking-[0.14em] text-muted-foreground">
                    Projects ({report.snapshots.length})
                  </CardTitle>
                </CardHeader>
                <CardContent>
                  {report.snapshots.length === 0 ? (
                    <EmptyState
                      icon={BarChart3}
                      title="No project snapshots"
                      description="Snapshots will appear once projects exist for the period."
                      actionLabel="Open Projects"
                      actionHref="/projects"
                      className="p-5"
                    />
                  ) : (
                    <div className="overflow-x-auto">
                      <table className="w-full text-sm">
                        <thead>
                          <tr className="border-b">
                            <th className="px-3 py-2 text-left text-xs font-medium uppercase tracking-[0.14em] text-muted-foreground">
                              Project
                            </th>
                            <th className="px-3 py-2 text-left text-xs font-medium uppercase tracking-[0.14em] text-muted-foreground">
                              Client
                            </th>
                            <th className="hidden px-3 py-2 text-left text-xs font-medium uppercase tracking-[0.14em] text-muted-foreground md:table-cell">
                              Milestone
                            </th>
                            <th className="px-3 py-2 text-left text-xs font-medium uppercase tracking-[0.14em] text-muted-foreground">
                              Status
                            </th>
                            <th className="px-3 py-2 text-left text-xs font-medium uppercase tracking-[0.14em] text-muted-foreground">
                              Ball With
                            </th>
                          </tr>
                        </thead>
                        <tbody>
                          {report.snapshots.map((s) => (
                            <tr
                              key={s.project_id}
                              className="border-b border-border/50 transition-colors hover:bg-muted/30"
                            >
                              <td className="px-3 py-2">
                                <Link
                                  href={`/projects/${s.project_id}`}
                                  className="font-mono text-xs font-medium text-primary hover:underline"
                                >
                                  {s.project_id}
                                </Link>
                              </td>
                              <td className="px-3 py-2 text-xs text-muted-foreground">
                                {s.client_name ?? "—"}
                              </td>
                              <td className="hidden px-3 py-2 text-xs text-muted-foreground md:table-cell">
                                {s.current_milestone ?? "—"}
                              </td>
                              <td className="px-3 py-2">
                                <StatusBadge value={s.project_status} />
                              </td>
                              <td className="px-3 py-2">
                                <StatusBadge value={s.ball_with} />
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </CardContent>
              </Card>
            </div>
          )}
        </>
      ) : null}
    </div>
  );
}

function ReportSkeleton() {
  return (
    <div className="space-y-4">
      <Skeleton className="h-4 w-48" />
      <Card className="border border-border/80 bg-card shadow-sm">
        <CardContent className="space-y-3 p-4">
          {Array.from({ length: 8 }).map((_, i) => (
            <Skeleton key={i} className="h-4 w-full" />
          ))}
        </CardContent>
      </Card>
    </div>
  );
}
