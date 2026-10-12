"use client";

import { Suspense } from "react";
import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import {
  ArrowRight,
  CalendarDays,
  Search,
  X,
} from "lucide-react";

import { DataTable, type DataTableColumn } from "@/components/data-table";
import { PageHeader } from "@/components/page-header";
import { PageToolbar, ToolbarField } from "@/components/page-toolbar";
import { Pagination } from "@/components/pagination";
import { ResponsiveListItem } from "@/components/responsive-list";
import { ErrorPanel, LoadingPanel, StatePanel } from "@/components/state-panel";
import { StatusBadge } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { MILESTONE_SHORT } from "@/lib/milestones";
import { formatDate, formatTimestamp } from "@/lib/ui-format";
import { getUiTimeline, uiV2Keys } from "@/lib/ui-v2-api";
import type { UiTimelineFilters, UiTimelineItem } from "@/lib/ui-v2-types";
import { pageFromSearchParam, updateSearchParams } from "@/lib/url-state";
import { cn } from "@/lib/utils";

const PAGE_SIZE = 25;
const MILESTONE_ORDER = [
  "M1_NetworkSetup",
  "M2_UATSessionConfiguration",
  "M3_UATEnvironmentAccess",
  "M4_ApplicationTesting",
  "M5_ConformanceTestCases",
  "M6_TestReportAndProductionPreparation",
  "M7_LogReviewAndProductionSessionDelivery",
  "M8_GoLive",
] as const;

export default function TimelinePage() {
  return (
    <Suspense fallback={<LoadingPanel label="Loading project timeline" />}>
      <TimelineContent />
    </Suspense>
  );
}

function TimelineContent() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const filters: UiTimelineFilters = {
    q: searchParams.get("q") || undefined,
    health: (searchParams.get("health") || undefined) as UiTimelineFilters["health"],
    schedule_status: (searchParams.get("schedule_status") || undefined) as UiTimelineFilters["schedule_status"],
    sort: (searchParams.get("sort") || "milestone") as NonNullable<UiTimelineFilters["sort"]>,
    page: pageFromSearchParam(searchParams.get("page")),
    page_size: PAGE_SIZE,
  };
  const query = useQuery({
    queryKey: uiV2Keys.timeline(filters),
    queryFn: () => getUiTimeline(filters),
  });
  const data = query.data;
  const hasFilters = Boolean(filters.q || filters.health || filters.schedule_status || filters.sort !== "milestone");

  function replace(updates: Record<string, string | number | null | undefined>) {
    const next = updateSearchParams(searchParams, {
      ...updates,
      page: updates.page ?? null,
    });
    router.replace(`${pathname}${next ? `?${next}` : ""}`, { scroll: false });
  }

  return (
    <div className="mx-auto max-w-[1600px] space-y-6">
      <PageHeader
        eyebrow="Projects"
        title="Delivery timeline"
        description="Track milestone progress, operational health, and go-live timing as separate delivery signals."
        metadata={data ? <>{data.total} active projects <span aria-hidden="true">•</span> Updated {formatTimestamp(data.generated_at)}</> : undefined}
        actions={
          <Button variant="outline" render={<Link href="/projects" />}>
            Open projects
            <ArrowRight className="size-4" />
          </Button>
        }
      />

      <PageToolbar summary={data ? `${data.total} projects in this view` : "Loading project count"}>
        <form
          className="flex min-w-0 flex-1 gap-2 sm:max-w-md"
          onSubmit={(event) => {
            event.preventDefault();
            const value = new FormData(event.currentTarget).get("q")?.toString().trim();
            replace({ q: value || null });
          }}
        >
          <ToolbarField label="Search timeline" className="min-w-0 flex-1">
            <Input
              key={filters.q ?? ""}
              name="q"
              defaultValue={filters.q}
              placeholder="Project, client, or milestone"
            />
          </ToolbarField>
          <Button type="submit" variant="outline" className="mt-[1.375rem]" aria-label="Search timeline">
            <Search className="size-4" />
          </Button>
        </form>

        <ToolbarField label="Health" className="sm:w-40">
          <Select
            value={filters.health ?? "all"}
            onValueChange={(value) => replace({ health: value === "all" ? null : value })}
          >
            <SelectTrigger><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All health</SelectItem>
              <SelectItem value="red">Needs attention</SelectItem>
              <SelectItem value="yellow">Monitor</SelectItem>
              <SelectItem value="green">On track</SelectItem>
            </SelectContent>
          </Select>
        </ToolbarField>

        <ToolbarField label="Schedule" className="sm:w-40">
          <Select
            value={filters.schedule_status ?? "all"}
            onValueChange={(value) => replace({ schedule_status: value === "all" ? null : value })}
          >
            <SelectTrigger><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All schedules</SelectItem>
              <SelectItem value="overdue">Overdue</SelectItem>
              <SelectItem value="due_soon">Due soon</SelectItem>
              <SelectItem value="scheduled">Scheduled</SelectItem>
              <SelectItem value="no_date">No date</SelectItem>
              <SelectItem value="invalid">Invalid date</SelectItem>
            </SelectContent>
          </Select>
        </ToolbarField>

        <ToolbarField label="Sort by" className="sm:w-44">
          <Select value={filters.sort} onValueChange={(value) => replace({ sort: value === "milestone" ? null : value })}>
            <SelectTrigger><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="milestone">Milestone progress</SelectItem>
              <SelectItem value="go_live">Go-live date</SelectItem>
              <SelectItem value="risk">Delivery risk</SelectItem>
              <SelectItem value="name">Client name</SelectItem>
            </SelectContent>
          </Select>
        </ToolbarField>

        {hasFilters ? (
          <Button type="button" variant="ghost" onClick={() => router.replace(pathname, { scroll: false })}>
            <X className="size-4" />
            Clear
          </Button>
        ) : null}
      </PageToolbar>

      {query.isPending ? <LoadingPanel label="Loading project timeline" /> : null}
      {query.isError ? (
        <ErrorPanel
          description={query.error instanceof Error ? query.error.message : "The project timeline could not be loaded."}
          onRetry={() => void query.refetch()}
        />
      ) : null}

      {data ? (
        <section className="overflow-hidden rounded-xl border bg-card shadow-xs" aria-label="Project delivery timeline">
          <DataTable
            items={data.items ?? []}
            columns={columns}
            getRowKey={(project) => project.project_id}
            caption="Active projects by milestone and go-live timing"
            mobileRenderer={(project) => <TimelineMobileItem project={project} />}
            empty={
              <StatePanel
                title={hasFilters ? "No projects match these filters" : "No active projects"}
                description={hasFilters ? "Clear a filter or broaden the search to see more projects." : "Active projects will appear here after they are created."}
                icon={hasFilters ? Search : CalendarDays}
                className="m-4"
              />
            }
          />
          <Pagination
            page={data.page}
            pageSize={data.page_size}
            total={data.total}
            onPageChange={(page) => replace({ page })}
          />
        </section>
      ) : null}
    </div>
  );
}

const columns: DataTableColumn<UiTimelineItem>[] = [
  {
    id: "project",
    header: "Project",
    className: "min-w-52",
    cell: (project) => (
      <div>
        <Link href={`/projects/${project.project_id}`} className="font-mono text-sm font-semibold text-primary hover:underline">
          {project.project_id}
        </Link>
        <p className="mt-1 text-xs text-muted-foreground">{project.client_name || "Client not set"}</p>
        {project.project_tier ? <p className="mt-1 text-xs capitalize text-muted-foreground">{project.project_tier} tier</p> : null}
      </div>
    ),
  },
  {
    id: "milestone",
    header: "Milestone progress",
    className: "min-w-72",
    cell: (project) => <MilestoneProgress currentMilestone={project.current_milestone} />,
  },
  {
    id: "health",
    header: "Health",
    cell: (project) => <StatusBadge value={project.health} label={healthLabel(project.health)} />,
  },
  {
    id: "owner",
    header: "Ball with",
    cell: (project) => <StatusBadge value={project.ball_with || "Unknown"} />,
  },
  {
    id: "schedule",
    header: "Go-live",
    className: "min-w-40",
    cell: (project) => <ScheduleSummary project={project} />,
  },
  {
    id: "open",
    header: <span className="sr-only">Open</span>,
    className: "text-right",
    cell: (project) => (
      <Button
        size="icon-sm"
        variant="ghost"
        render={<Link href={`/projects/${project.project_id}`} aria-label={`Open ${project.project_id}`} />}
      >
        <ArrowRight className="size-4" />
      </Button>
    ),
  },
];

function TimelineMobileItem({ project }: { project: UiTimelineItem }) {
  return (
    <ResponsiveListItem
      title={
        <Link href={`/projects/${project.project_id}`} className="font-mono text-primary hover:underline">
          {project.project_id} <span className="font-sans text-foreground">· {project.client_name || "Client not set"}</span>
        </Link>
      }
      metadata={project.project_tier ? `${project.project_tier} tier` : "Project tier not set"}
      status={<StatusBadge value={project.health} label={healthLabel(project.health)} />}
      action={
        <Button size="sm" variant="outline" render={<Link href={`/projects/${project.project_id}`} />}>
          Open project
          <ArrowRight className="size-4" />
        </Button>
      }
    >
      <MilestoneProgress currentMilestone={project.current_milestone} />
      <div className="mt-3 flex flex-wrap gap-2">
        <StatusBadge value={project.ball_with || "Unknown"} />
        <ScheduleBadge status={project.schedule_status} />
      </div>
      <p className="mt-3 text-xs">Target go-live: {formatDate(project.target_go_live)}</p>
      <p className="mt-1 text-xs">{scheduleDetail(project)}</p>
    </ResponsiveListItem>
  );
}

function MilestoneProgress({ currentMilestone }: { currentMilestone: string | null | undefined }) {
  const currentIndex = MILESTONE_ORDER.findIndex((milestone) => milestone === currentMilestone);
  const milestone = currentMilestone ? MILESTONE_SHORT[currentMilestone] : undefined;
  const accessibleLabel = milestone
    ? `${milestone.short} ${milestone.label}; ${currentIndex + 1} of ${MILESTONE_ORDER.length} milestones reached`
    : "No milestone recorded";

  return (
    <div className="min-w-0" aria-label={accessibleLabel} role="img">
      <div className="mb-2 flex items-center justify-between gap-3 text-xs">
        <span className="font-medium text-foreground">{milestone ? `${milestone.short} · ${milestone.label}` : "Not set"}</span>
        <span className="shrink-0 font-mono text-muted-foreground">{Math.max(currentIndex + 1, 0)}/8</span>
      </div>
      <div className="grid grid-cols-8 gap-1" aria-hidden="true">
        {MILESTONE_ORDER.map((item, index) => (
          <span
            key={item}
            className={cn(
              "h-1.5 rounded-full bg-muted",
              index < currentIndex && "bg-primary/45",
              index === currentIndex && "bg-primary",
            )}
          />
        ))}
      </div>
    </div>
  );
}

function ScheduleSummary({ project }: { project: UiTimelineItem }) {
  return (
    <div className="space-y-1.5">
      <ScheduleBadge status={project.schedule_status} />
      <p className="text-xs text-foreground">{formatDate(project.target_go_live)}</p>
      <p className="text-xs text-muted-foreground">{scheduleDetail(project)}</p>
    </div>
  );
}

function ScheduleBadge({ status }: { status: UiTimelineItem["schedule_status"] }) {
  return <StatusBadge value={scheduleStatusValue(status)} label={scheduleStatusLabel(status)} />;
}

function healthLabel(health: UiTimelineItem["health"]): string {
  if (health === "red") return "Needs attention";
  if (health === "yellow") return "Monitor";
  return "On track";
}

function scheduleStatusValue(status: UiTimelineItem["schedule_status"]): string {
  if (status === "overdue" || status === "invalid") return "red";
  if (status === "due_soon") return "yellow";
  if (status === "scheduled") return "green";
  return "Unknown";
}

function scheduleStatusLabel(status: UiTimelineItem["schedule_status"]): string {
  const labels: Record<UiTimelineItem["schedule_status"], string> = {
    overdue: "Overdue",
    due_soon: "Due soon",
    scheduled: "Scheduled",
    no_date: "No date",
    invalid: "Invalid date",
  };
  return labels[status];
}

function scheduleDetail(project: UiTimelineItem): string {
  const days = project.days_to_go_live;
  if (project.schedule_status === "overdue" && days !== null && days !== undefined) {
    const count = Math.abs(days);
    return `${count} day${count === 1 ? "" : "s"} overdue`;
  }
  if (project.schedule_status === "due_soon" && days !== null && days !== undefined) {
    if (days === 0) return "Go-live is today";
    return `${days} day${days === 1 ? "" : "s"} remaining`;
  }
  if (project.schedule_status === "scheduled" && days !== null && days !== undefined) {
    return `${days} days remaining`;
  }
  if (project.schedule_status === "invalid") return "Correct the stored target date";
  return "Set a target date to track timing";
}
