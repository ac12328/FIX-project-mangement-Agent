"use client";

import { useCallback, useEffect, useRef, useState, type CSSProperties } from "react";
import Link from "next/link";
import { ArrowLeft, RefreshCw, ShieldCheck } from "lucide-react";
import { PageHeader } from "@/components/page-header";
import { ErrorPanel, LoadingPanel } from "@/components/state-panel";
import { Button, buttonVariants } from "@/components/ui/button";
import { ProjectMilestoneOverview } from "@/components/reports/project-gantt";
import { ProjectReportExport } from "@/components/reports/project-report-export";
import { ProjectReportTable } from "@/components/reports/project-report-table";
import { getProjectSummaryReport } from "@/lib/api";
import { REPORT_EVENTS, formatReportSnapshot, type ProjectReport } from "@/lib/project-report";
import { useEvents } from "@/lib/useEvents";

export function ProjectSummaryReportPage() {
  const [report, setReport] = useState<ProjectReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [mine, setMine] = useState(false);
  const [outdated, setOutdated] = useState(false);
  const [exporting, setExporting] = useState(false);
  const svgRef = useRef<SVGSVGElement>(null);
  const requestId = useRef(0);
  const eventVersion = useRef(0);
  const markOutdated = useCallback(() => { eventVersion.current += 1; setOutdated(true); }, []);
  useEvents({ eventTypes: REPORT_EVENTS, onEvent: markOutdated, onReconnect: markOutdated });
  const load = useCallback(async () => {
    const id = ++requestId.current, before = eventVersion.current;
    setLoading(true); setError(null);
    try {
      const snapshot = await getProjectSummaryReport(mine);
      if (requestId.current === id) { setReport(snapshot); setOutdated(eventVersion.current !== before); }
    } catch (cause) {
      if (requestId.current === id) setError(cause instanceof Error ? cause.message : "Could not load the report.");
    } finally { if (requestId.current === id) setLoading(false); }
  }, [mine]);
  useEffect(() => {
    void load();
    return () => { requestId.current += 1; };
  }, [load]);
  const visibleReport = report && report.scope === (mine ? "mine" : "all") ? report : null;
  return <div className="mx-auto max-w-[1600px] space-y-5" style={{ "--primary": "var(--accent-foreground)" } as CSSProperties}>
    <PageHeader title="Project summary" className="border-0 pb-0"
      description="Milestones, owners and next actions in one shareable snapshot."
      metadata={visibleReport ? <span title={visibleReport.generated_at}>Snapshot / {formatReportSnapshot(visibleReport.generated_at)}</span> : undefined}
      actions={<><Link className={buttonVariants({ variant: "ghost", size: "sm" })} href="/reports"><ArrowLeft className="size-3.5" />Other reports</Link><Button variant="outline" size="sm" disabled={loading || exporting} onClick={() => void load()}><RefreshCw className="size-4" />Refresh snapshot</Button></>} />
    {visibleReport && <nav aria-label="Report sections" className="grid grid-cols-3 gap-2 sm:gap-3">
      {[
        { id: "active-projects", label: "Active", count: visibleReport.active.length, detail: "Active + Blocked", color: "border-primary/35" },
        { id: "postponed-projects", label: "Postponed", count: visibleReport.postponed.length, detail: "PM-paused projects", color: "border-border" },
        { id: "completed-projects", label: "Completed", count: visibleReport.completed.length, detail: "Last 14 days", color: "border-border" },
      ].map(section => <a key={section.id} href={`#${section.id}`} className={`flex min-w-0 flex-col items-start justify-between gap-1 rounded-xl border bg-card px-3 py-3 transition-colors hover:bg-muted/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring sm:flex-row sm:items-center sm:px-4 ${section.color}`}>
        <div><span className="text-xs font-semibold sm:text-sm">{section.label}</span><span className="mt-0.5 hidden text-xs text-muted-foreground sm:block">{section.detail}</span></div>
        <span className="text-2xl font-semibold tabular-nums">{section.count}</span>
      </a>)}
    </nav>}
    <div className="flex flex-col justify-between gap-3 rounded-xl border bg-card p-3 xl:flex-row xl:items-start">
      <div className="inline-flex w-fit gap-1 rounded-lg bg-muted/50 p-1" role="group" aria-label="Report scope">
        <Button size="sm" variant={mine ? "ghost" : "secondary"} aria-pressed={!mine} disabled={loading || exporting} onClick={() => setMine(false)}>All projects</Button>
        <Button size="sm" variant={mine ? "secondary" : "ghost"} aria-pressed={mine} disabled={loading || exporting} onClick={() => setMine(true)}>My projects</Button>
      </div>
      {visibleReport && <ProjectReportExport report={visibleReport} svgRef={svgRef} disabled={loading || !!error} onBusyChange={setExporting} />}
    </div>
    {outdated && <div role="status" className="rounded-xl border border-warning/40 bg-warning/10 p-3 text-sm">Project data may have changed. This report remains frozen so the image and Markdown match. Refresh when ready for a new snapshot.</div>}
    {error && <ErrorPanel description={`${error}${visibleReport ? " The previous snapshot is still displayed; refresh before exporting again." : ""}`} onRetry={() => void load()} />}
    {loading && !visibleReport && <LoadingPanel label="Loading project reporting snapshot" />}
    {visibleReport && <>
      <section id="active-projects" aria-labelledby="active-title" className="scroll-mt-24">
        <ProjectMilestoneOverview key={`${visibleReport.generated_at}:${visibleReport.scope}`} report={visibleReport} svgRef={svgRef} />
      </section>
      <section id="postponed-projects" aria-labelledby="postponed-title" className="scroll-mt-24 space-y-3 pt-3">
        <div><h2 id="postponed-title" className="text-lg font-semibold tracking-tight">Postponed projects <span className="ml-1 text-sm font-normal text-muted-foreground">{visibleReport.postponed.length}</span></h2><p className="mt-1 text-sm text-muted-foreground">Paused by PM decision. Previous targets are not current commitments.</p></div>
        <ProjectReportTable items={visibleReport.postponed} kind="postponed" />
        <p className="text-xs leading-5 text-muted-foreground">Pause reasons and restart dates are not available as structured fields in this report.</p>
      </section>
      <section id="completed-projects" aria-labelledby="completed-title" className="scroll-mt-24 space-y-3 pt-3">
        <div><h2 id="completed-title" className="text-lg font-semibold tracking-tight">Completed / Last 14 days <span className="ml-1 text-sm font-normal text-muted-foreground">{visibleReport.completed.length}</span></h2><p className="mt-1 text-sm text-muted-foreground">{visibleReport.completed_from} to {visibleReport.completed_through} (HKT), through snapshot time. Accepted completion, not go-live.</p></div>
        <ProjectReportTable items={visibleReport.completed} kind="completed" />
        {!!visibleReport.warnings?.length && <details className="rounded-xl border border-warning/35 bg-warning/5 px-4 py-1">
          <summary className="cursor-pointer py-3 text-sm font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">Data quality notes ({visibleReport.warnings.length})</summary>
          <div className="space-y-2 pb-3">{visibleReport.warnings.map(warning => <p key={warning} className="text-sm leading-6 text-muted-foreground">{warning}</p>)}</div>
        </details>}
      </section>
      <p className="flex items-center gap-2 border-t pt-4 text-xs leading-5 text-muted-foreground"><ShieldCheck className="size-4 shrink-0" />Read-only snapshot. Internal, Archived and Cancelled projects are excluded.</p>
    </>}
  </div>;
}
