"use client";

import { Suspense } from "react";
import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { ArrowRight, CheckCircle2, CircleAlert, Clock3, Search, X } from "lucide-react";

import { DataTable, type DataTableColumn } from "@/components/data-table";
import { MetricCard } from "@/components/metric-card";
import { PageHeader } from "@/components/page-header";
import { PageToolbar, ToolbarField } from "@/components/page-toolbar";
import { Pagination } from "@/components/pagination";
import { ResponsiveListItem } from "@/components/responsive-list";
import { ErrorPanel, LoadingPanel, StatePanel } from "@/components/state-panel";
import { StatusBadge } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { formatDate, formatTimestamp } from "@/lib/ui-format";
import { getUiProjects, uiV2Keys } from "@/lib/ui-v2-api";
import type { UiProject, UiProjectFilters, UiProjectsResponse } from "@/lib/ui-v2-types";
import { pageFromSearchParam, updateSearchParams } from "@/lib/url-state";

const PAGE_SIZE = 25;

export default function PortfolioPage() {
  return <Suspense fallback={<LoadingPanel label="Loading portfolio health" />}><PortfolioContent /></Suspense>;
}

function PortfolioContent() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const filters: UiProjectFilters = {
    q: searchParams.get("q") || undefined,
    ball_with: searchParams.get("ball_with") || undefined,
    health: (searchParams.get("health") || undefined) as UiProjectFilters["health"],
    sort: (searchParams.get("sort") || "risk") as NonNullable<UiProjectFilters["sort"]>,
    active_only: true,
    page: pageFromSearchParam(searchParams.get("page")),
    page_size: PAGE_SIZE,
  };
  const summaryFilters: UiProjectFilters = { active_only: true, sort: "risk", page: 1, page_size: 1 };
  const summaryQuery = useQuery({ queryKey: uiV2Keys.projects(summaryFilters), queryFn: () => getUiProjects(summaryFilters) });
  const query = useQuery({ queryKey: uiV2Keys.projects(filters), queryFn: () => getUiProjects(filters) });
  const data = query.data;
  const summary = healthCounts(summaryQuery.data);

  function replace(updates: Record<string, string | number | null | undefined>) {
    const next = updateSearchParams(searchParams, { ...updates, page: updates.page ?? null });
    router.replace(`${pathname}${next ? `?${next}` : ""}`, { scroll: false });
  }

  return (
    <div className="mx-auto max-w-[1600px] space-y-6">
      <PageHeader
        eyebrow="Projects"
        title="Portfolio health"
        description="Prioritize projects by operational risk, ownership, inactivity, and the next committed action."
        metadata={data ? <>{data.total} matching projects <span aria-hidden="true">•</span> Updated {formatTimestamp(data.generated_at)}</> : undefined}
      />

      {summaryQuery.isError ? <ErrorPanel description="Portfolio metrics could not be refreshed. The worklist may still be available." onRetry={() => void summaryQuery.refetch()} /> : null}
      <section className="grid gap-3 sm:grid-cols-3" aria-label="Portfolio health metrics">
        <MetricCard label="Needs attention" value={summary.red} detail="Blocked or high-risk operational state" tone={summary.red ? "danger" : "neutral"} icon={CircleAlert} />
        <MetricCard label="Monitor" value={summary.yellow} detail="Waiting on external action" tone={summary.yellow ? "warning" : "neutral"} icon={Clock3} />
        <MetricCard label="On track" value={summary.green} detail="No active operational risk signal" tone="success" icon={CheckCircle2} />
      </section>

      <HealthDistribution {...summary} />

      <PageToolbar summary={data ? `${data.total} projects in this view` : "Loading project count"}>
        <form
          className="flex min-w-0 flex-1 gap-2 sm:max-w-md"
          onSubmit={(event) => {
            event.preventDefault();
            const value = new FormData(event.currentTarget).get("q")?.toString().trim();
            replace({ q: value || null });
          }}
        >
          <ToolbarField label="Search portfolio" className="min-w-0 flex-1">
            <Input key={filters.q ?? ""} name="q" defaultValue={filters.q} placeholder="Project ID, client, or market" />
          </ToolbarField>
          <Button type="submit" variant="outline" className="mt-[1.375rem]" aria-label="Search portfolio"><Search className="size-4" /></Button>
        </form>
        <ToolbarField label="Health" className="sm:w-40">
          <Select value={filters.health ?? "all"} onValueChange={(value) => replace({ health: value === "all" ? null : value })}>
            <SelectTrigger><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All health</SelectItem>
              <SelectItem value="red">Needs attention</SelectItem>
              <SelectItem value="yellow">Monitor</SelectItem>
              <SelectItem value="green">On track</SelectItem>
            </SelectContent>
          </Select>
        </ToolbarField>
        <ToolbarField label="Ball with" className="sm:w-40">
          <Select value={filters.ball_with ?? "all"} onValueChange={(value) => replace({ ball_with: value === "all" ? null : value })}>
            <SelectTrigger><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All owners</SelectItem>
              <SelectItem value="Internal">Internal</SelectItem>
              <SelectItem value="External">External</SelectItem>
              <SelectItem value="Client">Client</SelectItem>
              <SelectItem value="Shared">Shared</SelectItem>
              <SelectItem value="Unknown">Unknown</SelectItem>
            </SelectContent>
          </Select>
        </ToolbarField>
        <ToolbarField label="Sort" className="sm:w-44">
          <Select value={filters.sort} onValueChange={(value) => replace({ sort: value === "risk" ? null : value })}>
            <SelectTrigger><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="risk">Risk first</SelectItem>
              <SelectItem value="updated">Recently updated</SelectItem>
              <SelectItem value="go_live">Go-live date</SelectItem>
              <SelectItem value="name">Client name</SelectItem>
            </SelectContent>
          </Select>
        </ToolbarField>
        {filters.q || filters.health || filters.ball_with || filters.sort !== "risk" ? <Button variant="ghost" onClick={() => router.replace(pathname)}><X className="size-4" />Clear</Button> : null}
      </PageToolbar>

      {query.isPending ? <LoadingPanel label="Loading portfolio worklist" /> : null}
      {query.isError ? <ErrorPanel description={query.error instanceof Error ? query.error.message : "Portfolio health could not be loaded."} onRetry={() => void query.refetch()} /> : null}
      {data ? (
        <section className="overflow-hidden rounded-xl border bg-card shadow-xs" aria-label="Portfolio health worklist">
          <DataTable
            items={data.items ?? []}
            columns={columns}
            getRowKey={(project) => project.project_id}
            caption="Portfolio health worklist"
            mobileRenderer={(project) => <PortfolioMobileItem project={project} />}
            empty={<StatePanel title="No projects match these filters" description="Clear a filter or open the full projects list to adjust project data." icon={CheckCircle2} className="m-4" />}
          />
          <Pagination page={data.page} pageSize={data.page_size} total={data.total} onPageChange={(page) => replace({ page })} />
        </section>
      ) : null}
    </div>
  );
}

const columns: DataTableColumn<UiProject>[] = [
  {
    id: "project",
    header: "Project",
    className: "min-w-48",
    cell: (project) => <div><Link href={`/projects/${project.project_id}`} className="font-mono text-sm font-semibold text-primary hover:underline">{project.project_id}</Link><p className="mt-1 text-xs text-muted-foreground">{project.client_name || "Unassigned client"}</p></div>,
  },
  { id: "health", header: "Health", cell: (project) => <StatusBadge value={project.health} label={healthLabel(project.health)} /> },
  { id: "owner", header: "Ball with", cell: (project) => <StatusBadge value={project.ball_with} /> },
  { id: "milestone", header: "Milestone", cell: (project) => <span className="font-mono text-xs">{project.current_milestone || "Not set"}</span> },
  { id: "stale", header: "Inactive", cell: (project) => <span className="font-mono text-xs">{project.days_stale}d</span> },
  { id: "next", header: "Next action", className: "min-w-64", cell: (project) => <p className="line-clamp-2 text-xs leading-5 text-muted-foreground">{project.next_action || "No next action recorded"}</p> },
  { id: "target", header: "Target", cell: (project) => <span className="text-xs">{formatDate(project.target_go_live)}</span> },
  { id: "open", header: <span className="sr-only">Open</span>, className: "text-right", cell: (project) => <Button size="icon-sm" variant="ghost" render={<Link href={`/projects/${project.project_id}`} aria-label={`Open ${project.project_id}`} />}><ArrowRight className="size-4" /></Button> },
];

function PortfolioMobileItem({ project }: { project: UiProject }) {
  return (
    <ResponsiveListItem
      title={<Link href={`/projects/${project.project_id}`} className="font-mono text-primary hover:underline">{project.project_id} · {project.client_name || "Unassigned"}</Link>}
      metadata={`${project.current_milestone || "No milestone"} · ${project.days_stale} days inactive`}
      status={<StatusBadge value={project.health} label={healthLabel(project.health)} />}
      action={<Button size="sm" variant="outline" render={<Link href={`/projects/${project.project_id}`} />}>Open project <ArrowRight className="size-4" /></Button>}
    >
      <div className="flex flex-wrap gap-2"><StatusBadge value={project.project_status} showDot={false} /><StatusBadge value={project.ball_with} /></div>
      <p className="mt-3 leading-5">{project.next_action || "No next action recorded"}</p>
      <p className="mt-2 text-xs">Target: {formatDate(project.target_go_live)}</p>
    </ResponsiveListItem>
  );
}

function healthLabel(health: UiProject["health"]): string {
  if (health === "red") return "Needs attention";
  if (health === "yellow") return "Monitor";
  return "On track";
}

function healthCounts(data?: UiProjectsResponse): { red: number; yellow: number; green: number } {
  const values = data?.facets?.find((facet) => facet.id === "health")?.values ?? [];
  const count = (health: string) => values.find((item) => item.value === health)?.count ?? 0;
  return { red: count("red"), yellow: count("yellow"), green: count("green") };
}

function HealthDistribution({ red, yellow, green }: { red: number; yellow: number; green: number }) {
  const total = red + yellow + green;
  if (!total) return null;
  return (
    <section className="rounded-xl border bg-card p-4 shadow-xs" aria-label={`Portfolio distribution: ${red} need attention, ${yellow} monitor, ${green} on track`}>
      <div className="flex h-3 overflow-hidden rounded-full bg-muted">
        {red ? <span className="bg-danger" style={{ width: `${(red / total) * 100}%` }} /> : null}
        {yellow ? <span className="bg-warning" style={{ width: `${(yellow / total) * 100}%` }} /> : null}
        {green ? <span className="bg-success" style={{ width: `${(green / total) * 100}%` }} /> : null}
      </div>
      <div className="mt-3 flex flex-wrap gap-x-5 gap-y-2 text-xs text-muted-foreground">
        <span><span className="mr-1.5 inline-block size-2 rounded-full bg-danger" />Needs attention {red}</span>
        <span><span className="mr-1.5 inline-block size-2 rounded-full bg-warning" />Monitor {yellow}</span>
        <span><span className="mr-1.5 inline-block size-2 rounded-full bg-success" />On track {green}</span>
      </div>
    </section>
  );
}
