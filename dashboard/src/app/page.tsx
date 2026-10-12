"use client";

import { useCallback, useEffect, useRef, useState, type MouseEvent } from "react";
import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import {
  Activity,
  ArrowRight,
  CalendarDays,
  CircleAlert,
  ClipboardCheck,
  RefreshCw,
  ShieldCheck,
} from "lucide-react";

import { DataTable, type DataTableColumn } from "@/components/data-table";
import { MetricCard } from "@/components/metric-card";
import { PageHeader } from "@/components/page-header";
import { PageToolbar, ToolbarField } from "@/components/page-toolbar";
import { QuerySection } from "@/components/query-section";
import { Pagination } from "@/components/pagination";
import { ResponsiveListItem } from "@/components/responsive-list";
import { SectionPanel } from "@/components/section-panel";
import { ErrorPanel, LoadingPanel, StatePanel } from "@/components/state-panel";
import { StatusBadge } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useAuth } from "@/lib/auth-context";
import { formatAge, formatDate, formatTimestamp } from "@/lib/ui-format";
import { getUiOverview, getUiWorkItems, uiV2Keys } from "@/lib/ui-v2-api";
import { cn } from "@/lib/utils";
import {
  isWorkQueueRequested,
  updateWorkQueueLocation,
  WORK_QUEUE_FOCUS_EVENT,
  WORK_QUEUE_HREF,
  WORK_QUEUE_ID,
} from "@/lib/work-queue-navigation";
import type { UiActionItem, UiRiskItem, UiWorkFilters } from "@/lib/ui-v2-types";

const PAGE_SIZE = 8;

const metricIcons = {
  needs_attention: CircleAlert,
  my_work: ClipboardCheck,
  portfolio_health: ShieldCheck,
} as const;

export default function DashboardPage() {
  const { session } = useAuth();
  const [kind, setKind] = useState("all");
  const [priority, setPriority] = useState("all");
  const [sort, setSort] = useState<NonNullable<UiWorkFilters["sort"]>>("priority");
  const [page, setPage] = useState(1);
  const [queueHighlighted, setQueueHighlighted] = useState(false);
  const queueRef = useRef<HTMLDivElement>(null);
  const queueHighlightTimer = useRef<number | null>(null);
  const initialQueueRequestHandled = useRef(false);

  const revealWorkQueue = useCallback(() => {
    const queueElement = queueRef.current;
    if (!queueElement) return;

    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    queueElement.scrollIntoView({ behavior: reducedMotion ? "auto" : "smooth", block: "start" });
    queueElement.focus({ preventScroll: true });
    setQueueHighlighted(true);

    if (queueHighlightTimer.current !== null) window.clearTimeout(queueHighlightTimer.current);
    queueHighlightTimer.current = window.setTimeout(() => {
      setQueueHighlighted(false);
      queueHighlightTimer.current = null;
    }, 1600);
  }, []);

  const handleMyWorkClick = useCallback((event: MouseEvent<HTMLAnchorElement>) => {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    updateWorkQueueLocation();
    revealWorkQueue();
  }, [revealWorkQueue]);

  useEffect(() => {
    const handleFocusRequest = () => revealWorkQueue();
    window.addEventListener(WORK_QUEUE_FOCUS_EVENT, handleFocusRequest);
    return () => {
      window.removeEventListener(WORK_QUEUE_FOCUS_EVENT, handleFocusRequest);
      if (queueHighlightTimer.current !== null) window.clearTimeout(queueHighlightTimer.current);
    };
  }, [revealWorkQueue]);

  const overview = useQuery({
    queryKey: uiV2Keys.overview(),
    queryFn: getUiOverview,
  });
  const canExecute = overview.data?.can_execute ?? false;
  const queueFilters: UiWorkFilters = {
    kind: kind === "all" ? undefined : kind as UiWorkFilters["kind"],
    priority: priority === "all" ? undefined : priority as UiWorkFilters["priority"],
    sort,
    page,
    page_size: PAGE_SIZE,
  };
  const workItems = useQuery({
    queryKey: uiV2Keys.workItems(queueFilters),
    queryFn: () => getUiWorkItems(queueFilters),
    enabled: canExecute,
  });

  useEffect(() => {
    if (!overview.data || initialQueueRequestHandled.current || !isWorkQueueRequested(window.location)) return;
    const frame = window.requestAnimationFrame(() => {
      if (!queueRef.current) return;
      initialQueueRequestHandled.current = true;
      revealWorkQueue();
    });
    return () => window.cancelAnimationFrame(frame);
  }, [overview.data, revealWorkQueue]);

  if (overview.isPending) return <LoadingPanel label="Loading operational overview" />;
  if (!overview.data) {
    return <ErrorPanel description={overview.error instanceof Error ? overview.error.message : "The operational overview is unavailable."} onRetry={() => void overview.refetch()} />;
  }

  const data = overview.data;
  const sectionStatus = new Map((data.section_status ?? []).map((section) => [section.section, section]));
  const riskError = sectionStatus.get("risk_summary")?.state === "error";
  const upcomingError = sectionStatus.get("upcoming")?.state === "error";
  const queueError = sectionStatus.get("action_queue")?.state === "error";
  const risks = data.risk_summary?.items ?? [];
  const upcoming = data.upcoming ?? [];
  const queue = workItems.data?.items ?? [];
  const refreshing = overview.isFetching || (canExecute && workItems.isFetching);
  const refreshWorkspace = () => Promise.all([
    overview.refetch(),
    ...(canExecute ? [workItems.refetch()] : []),
  ]);

  return (
    <div className="mx-auto max-w-[1600px] space-y-6">
      <PageHeader
        eyebrow="Today"
        title={`Welcome back, ${session?.display_name || session?.email || "operator"}`}
        description="What needs a decision today."
        metadata={<>
          <span>Overview updated {formatTimestamp(data.generated_at)}</span>
          {canExecute && workItems.data ? <span>Queue updated {formatTimestamp(workItems.data.generated_at)}</span> : null}
          {refreshing ? <span role="status">Refreshing workspace?</span> : null}
        </>}
        actions={
          <Button variant="outline" onClick={() => void refreshWorkspace()} disabled={refreshing}>
            <RefreshCw className={refreshing ? "size-4 animate-spin" : "size-4"} />
            Refresh
          </Button>
        }
      />

      {overview.isError ? (
        <div role="alert"><ErrorPanel description="The overview could not be refreshed. The previous snapshot is still shown and may be out of date." onRetry={() => void refreshWorkspace()} /></div>
      ) : null}

      <section aria-label="Operational metrics" className="grid gap-4 md:grid-cols-3">
        {(data.metrics ?? []).slice(0, 3).map((metric) => (
          <MetricCard
            key={metric.id}
            label={metric.label}
            value={(metric.id === "my_work" ? queueError : riskError) ? "?" : metric.value}
            detail={(metric.id === "my_work" ? queueError : riskError) ? "This metric could not be refreshed" : metric.detail}
            tone={(metric.id === "my_work" ? queueError : riskError) ? "neutral" : metric.tone}
            href={metric.id === "my_work" ? WORK_QUEUE_HREF : metric.href}
            icon={metricIcons[metric.id as keyof typeof metricIcons] ?? Activity}
            onClick={metric.id === "my_work" ? handleMyWorkClick : undefined}
          />
        ))}
      </section>

      {canExecute ? (
        <div
          id={WORK_QUEUE_ID}
          ref={queueRef}
          tabIndex={-1}
          role="region"
          aria-label="Action queue"
          data-highlighted={queueHighlighted ? "true" : "false"}
          className={cn(
            "scroll-mt-20 rounded-xl outline-none transition-shadow duration-200",
            queueHighlighted && "ring-3 ring-primary/30 ring-offset-2 ring-offset-background",
          )}
        >
          <SectionPanel
            title="Action queue"
          description="Reviews, drafts, approvals, and follow-ups ordered by operational urgency."
          action={<Button variant="ghost" render={<Link href="/reviews" />}>Open review workspace <ArrowRight className="size-4" /></Button>}
          contentClassName="space-y-4 pt-4"
        >
          <PageToolbar summary={workItems.data ? `${workItems.data.total} work items` : workItems.isError ? "Work item count unavailable" : "Loading work item count"}>
            <ToolbarField label="Work type" className="sm:w-44">
              <Select value={kind} onValueChange={(value) => { setKind(value ?? "all"); setPage(1); }}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All work</SelectItem>
                  <SelectItem value="review">Reviews</SelectItem>
                  <SelectItem value="draft">Drafts</SelectItem>
                  <SelectItem value="approval">Approvals</SelectItem>
                  <SelectItem value="followup">Follow-ups</SelectItem>
                </SelectContent>
              </Select>
            </ToolbarField>
            <ToolbarField label="Priority" className="sm:w-40">
              <Select value={priority} onValueChange={(value) => { setPriority(value ?? "all"); setPage(1); }}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All priorities</SelectItem>
                  <SelectItem value="critical">Critical</SelectItem>
                  <SelectItem value="high">High</SelectItem>
                  <SelectItem value="medium">Medium</SelectItem>
                  <SelectItem value="low">Low</SelectItem>
                </SelectContent>
              </Select>
            </ToolbarField>
            <ToolbarField label="Sort" className="sm:w-40">
              <Select value={sort} onValueChange={(value) => { setSort((value ?? "priority") as NonNullable<UiWorkFilters["sort"]>); setPage(1); }}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="priority">Priority</SelectItem>
                  <SelectItem value="due">Due date</SelectItem>
                  <SelectItem value="oldest">Oldest first</SelectItem>
                </SelectContent>
              </Select>
            </ToolbarField>
          </PageToolbar>

          {queueError ? <ErrorPanel description={sectionStatus.get("action_queue")?.message || "The queue snapshot failed to load."} onRetry={() => void overview.refetch()} /> : null}
          <QuerySection label="Work items" query={workItems}>
            {workItems.data !== undefined ? (
              <>
                <DataTable
                  items={queue}
                  columns={queueColumns}
                  getRowKey={(item) => `${item.kind}-${item.id}`}
                  caption="Action queue"
                  mobileRenderer={(item) => <QueueMobileItem item={item} />}
                  empty={<StatePanel title="Queue clear" description="No work items match the current filters." icon={ClipboardCheck} />}
                />
                {workItems.data ? <Pagination page={workItems.data.page} pageSize={workItems.data.page_size} total={workItems.data.total} onPageChange={setPage} /> : null}
              </>
            ) : null}
          </QuerySection>
        </SectionPanel>
        </div>
      ) : null}

      <div className="grid gap-6 xl:grid-cols-[minmax(0,1.35fr)_minmax(22rem,0.65fr)]">
        <SectionPanel title="Project risk" description="Highest-risk active projects and the signals driving attention." action={<Button variant="ghost" render={<Link href="/operations?tab=health" />}>Operations <ArrowRight className="size-4" /></Button>} contentClassName="pt-4">
          {riskError ? <ErrorPanel description={sectionStatus.get("risk_summary")?.message || "Risk signals are unavailable."} onRetry={() => void overview.refetch()} /> : risks.length ? (
            <div className="divide-y rounded-xl border">
              {risks.slice(0, 8).map((risk) => <RiskRow key={risk.project_id} item={risk} />)}
            </div>
          ) : <StatePanel title="No active risk signals" description="The active portfolio has no projects currently flagged for attention." icon={ShieldCheck} />}
        </SectionPanel>

        <SectionPanel title="Upcoming timeline" description="Go-lives and follow-ups due in the next 30 days." action={<Button variant="ghost" render={<Link href="/operations?tab=timeline" />}>Full timeline <ArrowRight className="size-4" /></Button>} contentClassName="pt-4">
          {upcomingError ? <ErrorPanel description={sectionStatus.get("upcoming")?.message || "Upcoming commitments are unavailable."} onRetry={() => void overview.refetch()} /> : upcoming.length ? (
            <div className="space-y-1">
              {upcoming.slice(0, 8).map((item) => (
                <Link key={`${item.kind}-${item.id}`} href={item.href} className="flex min-h-16 items-start gap-3 rounded-lg px-2 py-2 transition-colors hover:bg-muted focus-visible:outline-none focus-visible:ring-3 focus-visible:ring-ring/50">
                  <span className="mt-0.5 flex size-9 shrink-0 items-center justify-center rounded-lg bg-primary/10 text-primary"><CalendarDays className="size-4" /></span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-sm font-medium">{item.title}</span>
                    <span className="mt-0.5 block truncate text-xs text-muted-foreground">{item.project_label} · {formatDate(item.due_at)}</span>
                  </span>
                  <span className="shrink-0 font-mono text-xs text-muted-foreground">{item.days_until === 0 ? "Today" : `${item.days_until}d`}</span>
                </Link>
              ))}
            </div>
          ) : <StatePanel title="Nothing scheduled" description="There are no go-lives or follow-ups due in the next 30 days." icon={CalendarDays} />}
        </SectionPanel>
      </div>
    </div>
  );
}

const queueColumns: DataTableColumn<UiActionItem>[] = [
  { id: "priority", header: "Priority", cell: (item) => <StatusBadge value={item.priority} /> },
  { id: "work", header: "Work item", className: "min-w-64", cell: (item) => <div><p className="font-medium">{item.title}</p><p className="mt-1 text-xs leading-5 text-muted-foreground">{item.reason}</p></div> },
  { id: "project", header: "Project", cell: (item) => item.project_id ? <Link className="font-mono text-xs text-primary hover:underline" href={`/projects/${item.project_id}`}>{item.project_label || item.project_id}</Link> : <span className="text-xs text-muted-foreground">Workspace</span> },
  { id: "timing", header: "Age / due", cell: (item) => <div className="text-xs"><p>{formatAge(item.age_days)}</p><p className="mt-1 text-muted-foreground">{item.due_at ? formatDate(item.due_at) : "No due date"}</p></div> },
  { id: "action", header: <span className="sr-only">Action</span>, className: "text-right", cell: (item) => item.can_execute ? <Button size="sm" variant="outline" render={<Link href={item.action_href} />}>{item.action_label}</Button> : null },
];

function QueueMobileItem({ item }: { item: UiActionItem }) {
  return <ResponsiveListItem title={item.title} status={<StatusBadge value={item.priority} />} metadata={`${item.kind} · ${formatAge(item.age_days)}`} action={item.can_execute ? <Button size="sm" render={<Link href={item.action_href} />}>{item.action_label}</Button> : undefined}><p>{item.reason}</p>{item.project_label ? <p className="mt-2 text-xs">Project: {item.project_label}</p> : null}</ResponsiveListItem>;
}

function RiskRow({ item }: { item: UiRiskItem }) {
  return (
    <Link href={`/projects/${item.project_id}`} className="grid min-h-20 gap-2 p-4 transition-colors hover:bg-muted/60 focus-visible:outline-none focus-visible:ring-3 focus-visible:ring-inset focus-visible:ring-ring/50 sm:grid-cols-[auto_minmax(0,1fr)_auto] sm:items-start">
      <StatusBadge value={item.health} />
      <span className="min-w-0">
        <span className="block font-medium">{item.project_label}</span>
        <span className="mt-1 block text-xs leading-5 text-muted-foreground">{item.reason}</span>
        {item.next_action ? <span className="mt-1 block text-xs">Next: {item.next_action}</span> : null}
      </span>
      <span className="font-mono text-xs text-muted-foreground">{item.days_inactive ?? 0}d inactive</span>
    </Link>
  );
}
