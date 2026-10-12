"use client";

import { Suspense } from "react";
import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { ArrowRight, CheckCircle2, Search, X } from "lucide-react";

import { DataTable, type DataTableColumn } from "@/components/data-table";
import { PageHeader } from "@/components/page-header";
import { PageToolbar, ToolbarField } from "@/components/page-toolbar";
import { Pagination } from "@/components/pagination";
import { ResponsiveListItem } from "@/components/responsive-list";
import { ErrorPanel, LoadingPanel, StatePanel } from "@/components/state-panel";
import { StatusBadge } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { MILESTONE_SHORT } from "@/lib/milestones";
import { formatAge, formatDate, formatTimestamp } from "@/lib/ui-format";
import { getUiCompletedProjects, uiV2Keys } from "@/lib/ui-v2-api";
import type { UiCompletedProject, UiCompletedProjectFilters } from "@/lib/ui-v2-types";
import { pageFromSearchParam, updateSearchParams } from "@/lib/url-state";

const PAGE_SIZE = 25;
const DEFAULT_DAYS = 30;

function windowDays(value: string | null): 14 | 30 {
  return value === "14" ? 14 : DEFAULT_DAYS;
}

function milestoneLabel(value: string | null | undefined): string {
  if (!value) return "Not set";
  const known = MILESTONE_SHORT[value];
  return known ? `${known.short} · ${known.label}` : value.replaceAll("_", " ");
}

export default function CompletedProjectsPanel() {
  return (
    <Suspense fallback={<LoadingPanel label="Loading completed projects" />}>
      <CompletedProjectsContent />
    </Suspense>
  );
}

function CompletedProjectsContent() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const days = windowDays(searchParams.get("completed_days"));
  const filters: UiCompletedProjectFilters = {
    days,
    q: searchParams.get("q") || undefined,
    page: pageFromSearchParam(searchParams.get("page")),
    page_size: PAGE_SIZE,
  };
  const query = useQuery({
    queryKey: uiV2Keys.completedProjects(filters),
    queryFn: () => getUiCompletedProjects(filters),
  });
  const data = query.data;

  function replace(updates: Record<string, string | number | null | undefined>) {
    const next = updateSearchParams(searchParams, { ...updates, page: updates.page ?? null });
    router.replace(`${pathname}${next ? `?${next}` : ""}`, { scroll: false });
  }

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow="Operations"
        title="Completed projects"
        description="Projects closed in the selected window, dated from the accepted Completed write — not last activity."
        metadata={data ? <>{data.total} completed <span aria-hidden="true">•</span> Last {days} days <span aria-hidden="true">•</span> Updated {formatTimestamp(data.generated_at)}</> : undefined}
      />

      <PageToolbar summary={data ? `${data.total} projects completed in the last ${days} days` : "Loading completed projects"}>
        <form
          className="flex min-w-0 flex-1 gap-2 sm:max-w-md"
          onSubmit={(event) => {
            event.preventDefault();
            const value = new FormData(event.currentTarget).get("q")?.toString().trim();
            replace({ q: value || null });
          }}
        >
          <ToolbarField label="Search completed projects" className="min-w-0 flex-1">
            <Input key={filters.q ?? ""} name="q" defaultValue={filters.q} placeholder="Project ID, client, or market" />
          </ToolbarField>
          <Button type="submit" variant="outline" className="mt-[1.375rem]" aria-label="Search completed projects">
            <Search className="size-4" />
          </Button>
        </form>
        <ToolbarField label="Completed in" className="sm:w-44">
          <Select
            value={String(days)}
            onValueChange={(value) => replace({ completed_days: value === "30" ? null : value })}
          >
            <SelectTrigger aria-label="Completed in">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="30">Last 30 days</SelectItem>
              <SelectItem value="14">Last 14 days</SelectItem>
            </SelectContent>
          </Select>
        </ToolbarField>
        {filters.q || days !== DEFAULT_DAYS ? (
          <Button variant="ghost" onClick={() => replace({ q: null, completed_days: null })}>
            <X className="size-4" />
            Clear
          </Button>
        ) : null}
      </PageToolbar>

      {query.isPending ? <LoadingPanel label="Loading completed projects" /> : null}
      {query.isError ? (
        <ErrorPanel
          description={query.error instanceof Error ? query.error.message : "Completed projects could not be loaded."}
          onRetry={() => void query.refetch()}
        />
      ) : null}
      {data ? (
        <section className="overflow-hidden rounded-xl border bg-card shadow-xs" aria-label="Completed projects">
          <DataTable
            items={data.items ?? []}
            columns={columns}
            getRowKey={(project) => project.project_id}
            caption="Completed projects"
            mobileRenderer={(project) => <CompletedMobileItem project={project} />}
            empty={
              <StatePanel
                title={`No projects completed in the last ${days} days`}
                description="Choose a wider window or open a project if you expected a recent close."
                icon={CheckCircle2}
                className="m-4"
              />
            }
          />
          <Pagination page={data.page} pageSize={data.page_size} total={data.total} onPageChange={(page) => replace({ page })} />
        </section>
      ) : null}
    </div>
  );
}

const columns: DataTableColumn<UiCompletedProject>[] = [
  {
    id: "project",
    header: "Project",
    className: "min-w-48",
    cell: (project) => (
      <div>
        <Link href={`/projects/${project.project_id}`} className="font-mono text-sm font-semibold text-primary hover:underline">
          {project.project_id}
        </Link>
        <p className="mt-1 text-xs text-muted-foreground">{project.client_name || "Unassigned client"}</p>
      </div>
    ),
  },
  { id: "market", header: "Market", cell: (project) => <span className="text-xs">{project.market || "Unspecified"}</span> },
  { id: "milestone", header: "Milestone", cell: (project) => <span className="font-mono text-xs">{milestoneLabel(project.current_milestone)}</span> },
  { id: "completed", header: "Completed", cell: (project) => <span className="text-xs">{formatDate(project.completed_at)}</span> },
  { id: "ago", header: "From now", cell: (project) => <span className="font-mono text-xs">{formatAge(project.days_since_completed)}</span> },
  { id: "status", header: "Status", cell: (project) => <StatusBadge value={project.project_status} /> },
  {
    id: "open",
    header: <span className="sr-only">Open</span>,
    className: "text-right",
    cell: (project) => (
      <Button size="icon-sm" variant="ghost" render={<Link href={`/projects/${project.project_id}`} aria-label={`Open ${project.project_id}`} />}>
        <ArrowRight className="size-4" />
      </Button>
    ),
  },
];

function CompletedMobileItem({ project }: { project: UiCompletedProject }) {
  return (
    <ResponsiveListItem
      title={(
        <Link href={`/projects/${project.project_id}`} className="font-mono text-primary hover:underline">
          {project.project_id} · {project.client_name || "Unassigned"}
        </Link>
      )}
      metadata={`${milestoneLabel(project.current_milestone)} · ${formatDate(project.completed_at)}`}
      status={<StatusBadge value={project.project_status} />}
      action={(
        <Button size="sm" variant="outline" render={<Link href={`/projects/${project.project_id}`} />}>
          Open project <ArrowRight className="size-4" />
        </Button>
      )}
    >
      <p className="leading-5">{formatAge(project.days_since_completed)} ago</p>
    </ResponsiveListItem>
  );
}
