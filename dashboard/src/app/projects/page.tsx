"use client";

import { Suspense } from "react";
import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { usePathname, useSearchParams } from "next/navigation";
import { ArrowRight, FolderKanban, Plus, Search, X } from "lucide-react";

import { DataTable, type DataTableColumn } from "@/components/data-table";
import { PageHeader } from "@/components/page-header";
import { PageToolbar, ToolbarField } from "@/components/page-toolbar";
import { Pagination } from "@/components/pagination";
import { PermissionBoundary } from "@/components/permission-boundary";
import { ResponsiveListItem } from "@/components/responsive-list";
import { ErrorPanel, LoadingPanel, StatePanel } from "@/components/state-panel";
import { StatusBadge } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { formatDate, formatTimestamp } from "@/lib/ui-format";
import { getUiProjects, uiV2Keys } from "@/lib/ui-v2-api";
import type { UiProject, UiProjectFilters } from "@/lib/ui-v2-types";
import { pageFromSearchParam, updateSearchParams } from "@/lib/url-state";

const PAGE_SIZE = 25;

export default function ProjectsPage() {
  return <Suspense fallback={<LoadingPanel label="Loading projects" />}><ProjectsContent /></Suspense>;
}

function ProjectsContent() {
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const scope = searchParams.get("scope") === "completed" || searchParams.get("status") === "Completed"
    ? "completed" : searchParams.get("scope") === "all" ? "all" : "active";
  const filters: UiProjectFilters = {
    q: searchParams.get("q") || undefined,
    status: scope === "completed" ? "Completed" : searchParams.get("status") || undefined,
    ball_with: searchParams.get("ball_with") || undefined,
    health: (scope === "completed" ? undefined : searchParams.get("health") || undefined) as UiProjectFilters["health"],
    sort: (searchParams.get("sort") || "risk") as NonNullable<UiProjectFilters["sort"]>,
    active_only: scope === "active",
    mine: searchParams.get("mine") === "1",
    page: pageFromSearchParam(searchParams.get("page")),
    page_size: PAGE_SIZE,
  };
  const query = useQuery({ queryKey: uiV2Keys.projects(filters), queryFn: () => getUiProjects(filters) });

  function replace(updates: Record<string, string | number | boolean | null | undefined>) {
    const next = updateSearchParams(searchParams, { ...updates, page: updates.page ?? null });
    window.history.replaceState(null, "", `${pathname}${next ? `?${next}` : ""}`);
  }

  const data = query.data;
  return (
    <div className="mx-auto max-w-[1600px] space-y-6">
      <PageHeader
        eyebrow="Projects"
        title="Projects"
        description="Open a project for truth, mail, and closing."
        metadata={data ? <>{data.total} projects <span aria-hidden="true">•</span> Updated {formatTimestamp(data.generated_at)}</> : undefined}
        actions={<PermissionBoundary permission="business.write"><Button render={<Link href="/intake" />}><Plus className="size-4" />New intake</Button></PermissionBoundary>}
      />

      <PageToolbar className="lg:flex-col lg:items-stretch" summary={data ? `${data.total} matching projects` : "Loading project count"}>
        <form
          className="flex w-full min-w-0 items-end gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            const value = new FormData(event.currentTarget).get("q")?.toString().trim();
            replace({ q: value || null });
          }}
        >
          <ToolbarField label="Search projects" className="min-w-0 flex-1">
            <Input key={filters.q ?? ""} name="q" defaultValue={filters.q} placeholder="Project ID, client, or market" />
          </ToolbarField>
          <Button type="submit" variant="outline" className="shrink-0" aria-label="Search projects"><Search className="size-4" /></Button>
        </form>
        <ToolbarField label="Health" className="sm:w-36">
          <Select disabled={scope === "completed"} value={filters.health ?? "all"} onValueChange={(value) => replace({ health: value === "all" ? null : value })}>
            <SelectTrigger><SelectValue /></SelectTrigger><SelectContent>
              <SelectItem value="all">All health</SelectItem><SelectItem value="red">At risk</SelectItem><SelectItem value="yellow">Watch</SelectItem><SelectItem value="green">On track</SelectItem>
            </SelectContent>
          </Select>
        </ToolbarField>
        <ToolbarField label="Ball with" className="sm:w-40">
          <Select value={filters.ball_with ?? "all"} onValueChange={(value) => replace({ ball_with: value === "all" ? null : value })}>
            <SelectTrigger><SelectValue /></SelectTrigger><SelectContent>
              <SelectItem value="all">Everyone</SelectItem><SelectItem value="Internal">Internal</SelectItem><SelectItem value="External">External</SelectItem><SelectItem value="Shared">Shared</SelectItem><SelectItem value="Unknown">Unknown</SelectItem>
            </SelectContent>
          </Select>
        </ToolbarField>
        <ToolbarField label="Owner" className="sm:w-36">
          <Select value={filters.mine ? "mine" : "all"} onValueChange={(value) => replace({ mine: value === "mine" ? "1" : null })}>
            <SelectTrigger><SelectValue /></SelectTrigger><SelectContent>
              <SelectItem value="all">All owners</SelectItem>
              <SelectItem value="mine">Mine</SelectItem>
            </SelectContent>
          </Select>
        </ToolbarField>
        <ToolbarField label="Scope" className="sm:w-40">
          <Select value={scope} onValueChange={(value) => replace({ scope: value === "active" ? null : value, status: null, health: value === "completed" ? null : filters.health })}>
            <SelectTrigger><SelectValue>{scope === "completed" ? "Completed only" : scope === "all" ? "All projects" : "Active only"}</SelectValue></SelectTrigger>
            <SelectContent><SelectItem value="active">Active only</SelectItem><SelectItem value="all">All projects</SelectItem><SelectItem value="completed">Completed only</SelectItem></SelectContent>
          </Select>
        </ToolbarField>
        <ToolbarField label="Sort" className="sm:w-40">
          <Select value={filters.sort} onValueChange={(value) => replace({ sort: value === "risk" ? null : value })}>
            <SelectTrigger><SelectValue /></SelectTrigger><SelectContent>
              <SelectItem value="risk">Risk first</SelectItem><SelectItem value="updated">Recently updated</SelectItem><SelectItem value="go_live">Target date</SelectItem><SelectItem value="name">Client name</SelectItem>
            </SelectContent>
          </Select>
        </ToolbarField>
        {filters.q || filters.health || filters.ball_with || filters.mine || !filters.active_only || filters.sort !== "risk" ? (
          <Button variant="ghost" className="sm:mb-0" onClick={() => window.history.replaceState(null, "", pathname)}><X className="size-4" />Clear</Button>
        ) : null}
      </PageToolbar>

      {query.isPending ? <LoadingPanel label="Loading project worklist" /> : null}
      {query.isError ? <ErrorPanel description={query.error instanceof Error ? query.error.message : "Projects could not be loaded."} onRetry={() => void query.refetch()} /> : null}
      {data ? (
        <section className="overflow-hidden rounded-xl border bg-card shadow-xs" aria-label="Projects">
          <DataTable
            items={data.items ?? []}
            columns={columns}
            getRowKey={(project) => project.project_id}
            caption="Project portfolio worklist"
            mobileRenderer={(project) => <ProjectMobileItem project={project} />}
            empty={<StatePanel title="No matching projects" description="Try clearing one or more filters, or create a new intake." icon={FolderKanban} className="m-4" />}
            className="border-0 [&_table]:table-fixed [&_table]:min-w-[1080px]"
          />
          <Pagination page={data.page} pageSize={data.page_size} total={data.total} onPageChange={(page) => replace({ page })} />
        </section>
      ) : null}
    </div>
  );
}

const columns: DataTableColumn<UiProject>[] = [
  { id: "project", header: "Project", className: "w-[15%] break-words", cell: (project) => <div><Link href={`/projects/${project.project_id}`} className="font-mono text-sm font-semibold text-primary hover:underline">{project.project_id}</Link><p className="mt-1 text-xs text-muted-foreground">{project.client_name || "Unassigned client"}</p></div> },
  { id: "health", header: "Health", className: "w-[8%]", cell: (project) => <ProjectHealth project={project} /> },
  { id: "status", header: "Status", className: "w-[11%]", cell: (project) => <StatusBadge value={project.project_status} showDot={false} /> },
  { id: "owner", header: "Ball with", className: "w-[12%]", cell: (project) => <StatusBadge value={project.ball_with} /> },
  { id: "milestone", header: "Milestone", className: "w-[13%] [overflow-wrap:anywhere]", cell: (project) => <span className="font-mono text-xs">{project.current_milestone || "Not set"}</span> },
  { id: "next", header: "Next action", className: "w-[17%] [overflow-wrap:anywhere]", cell: (project) => <p className="line-clamp-2 text-xs leading-5 text-muted-foreground">{project.next_action || "No next action recorded"}</p> },
  { id: "go-live", header: "Target", className: "w-[9%]", cell: (project) => <span className="text-xs">{formatDate(project.target_go_live)}</span> },
  { id: "go-live-date", header: "Go-live date", className: "w-[10%]", cell: (project) => <GoLiveDate project={project} /> },
  { id: "open", header: <span className="sr-only">Open</span>, className: "w-[5%] px-1 text-right", cell: (project) => <Button size="icon-sm" variant="ghost" render={<Link href={`/projects/${project.project_id}`} aria-label={`Open ${project.project_id}`} />}><ArrowRight className="size-4" /></Button> },
];

function ProjectMobileItem({ project }: { project: UiProject }) {
  return (
    <ResponsiveListItem
      title={<Link href={`/projects/${project.project_id}`} className="font-mono text-primary">{project.project_id} · {project.client_name || "Unassigned"}</Link>}
      metadata={`${project.market || "No market"} · ${project.current_milestone || "No milestone"}`}
      status={<ProjectHealth project={project} />}
      action={<Button size="sm" variant="outline" render={<Link href={`/projects/${project.project_id}`} />}>Open project <ArrowRight className="size-4" /></Button>}
    >
      <div className="flex flex-wrap gap-2"><StatusBadge value={project.project_status} showDot={false} /><StatusBadge value={project.ball_with} /></div>
      <p className="mt-3 leading-5">{project.next_action || "No next action recorded"}</p>
      <p className="mt-2 text-xs">Go-live date: <GoLiveDate project={project} /></p>
      <p className="mt-2 text-xs">Target: {formatDate(project.target_go_live)} · {project.days_stale} days inactive</p>
    </ResponsiveListItem>
  );
}

function ProjectHealth({ project }: { project: UiProject }) {
  if (["Completed", "Archived", "Cancelled"].includes(project.project_status)) {
    return <span title="Onboarding health does not apply to closed projects"><StatusBadge value="Closed" label="N/A" showDot={false} /></span>;
  }
  return <StatusBadge value={project.health} />;
}

function GoLiveDate({ project }: { project: UiProject }) {
  // Keep confirmed dates visible while a newly deployed API is restarting.
  const date = project.actual_go_live || project.go_live_date;
  const source = project.actual_go_live ? "confirmed" : project.go_live_date_source;
  if (!date || source === "unknown" || !source) {
    return <span className="text-xs text-muted-foreground">Not confirmed</span>;
  }
  const inferred = source === "m8_inferred";
  const explanation = inferred
    ? `Inferred from the first recorded entry into M8 (UTC): ${project.go_live_date_source_at}. This is a system record date, not a confirmed actual activation date.`
    : project.actual_go_live_source_quote || project.go_live_date_source_note || "Confirmed actual go-live date";
  return <span className="inline-flex flex-col gap-1 text-xs" title={`${explanation} Source: ${project.go_live_date_source_ref || project.actual_go_live_evidence_id || "confirmed record"}`}>
    <span>{formatDate(date)}</span>
    <span className={inferred ? "text-warning" : "text-success"}>
      {inferred ? "Inferred from M8" : "Confirmed"}
    </span>
  </span>;
}
