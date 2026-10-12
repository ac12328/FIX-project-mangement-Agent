"use client";

import { Suspense } from "react";
import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { ExternalLink, Search, ScrollText, X } from "lucide-react";

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
import { formatTimestamp } from "@/lib/ui-format";
import { getUiActivity, uiV2Keys } from "@/lib/ui-v2-api";
import type { UiActivityFilters, UiActivityItem } from "@/lib/ui-v2-types";
import { pageFromSearchParam, updateSearchParams } from "@/lib/url-state";

const PAGE_SIZE = 50;

export default function ActivityPage() {
  return <Suspense fallback={<LoadingPanel label="Loading activity" />}><ActivityContent /></Suspense>;
}

function ActivityContent() {
  const pathname = usePathname();
  const router = useRouter();
  const searchParams = useSearchParams();
  const filters: UiActivityFilters = {
    q: searchParams.get("q") || undefined,
    action_type: searchParams.get("action_type") || undefined,
    project_id: searchParams.get("project_id") || undefined,
    page: pageFromSearchParam(searchParams.get("page")),
    page_size: PAGE_SIZE,
  };
  const query = useQuery({ queryKey: uiV2Keys.activity(filters), queryFn: () => getUiActivity(filters) });

  function replace(updates: Record<string, string | number | null | undefined>) {
    const next = updateSearchParams(searchParams, { ...updates, page: updates.page ?? null });
    router.replace(`${pathname}${next ? `?${next}` : ""}`, { scroll: false });
  }

  const data = query.data;
  const actionTypes = data?.facets?.find((facet) => facet.id === "action_type")?.values ?? [];
  return (
    <div className="mx-auto max-w-[1500px] space-y-6">
      <PageHeader
        eyebrow="AI & Analytics"
        title="Activity log"
        description="A searchable operational audit trail across projects, people, system actions, and agent workflows."
        metadata={data ? <>{data.total} events <span aria-hidden="true">•</span> Updated {formatTimestamp(data.generated_at)}</> : undefined}
      />
      <PageToolbar summary={data ? `${data.total} matching events` : "Loading event count"}>
        <form className="flex min-w-0 flex-1 gap-2 sm:max-w-md" onSubmit={(event) => { event.preventDefault(); const q = new FormData(event.currentTarget).get("q")?.toString().trim(); replace({ q: q || null }); }}>
          <ToolbarField label="Search activity" className="min-w-0 flex-1"><Input key={filters.q ?? ""} name="q" defaultValue={filters.q} placeholder="Summary, actor, surface, or object" /></ToolbarField>
          <Button type="submit" variant="outline" className="mt-[1.375rem]" aria-label="Search activity"><Search className="size-4" /></Button>
        </form>
        <ToolbarField label="Action type" className="sm:w-56">
          <Select value={filters.action_type ?? "all"} onValueChange={(value) => replace({ action_type: value === "all" ? null : value })}>
            <SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectItem value="all">All action types</SelectItem>{actionTypes.map((option) => <SelectItem key={option.value} value={option.value}>{option.value} ({option.count})</SelectItem>)}</SelectContent>
          </Select>
        </ToolbarField>
        <form className="flex min-w-0 gap-2" onSubmit={(event) => { event.preventDefault(); const projectId = new FormData(event.currentTarget).get("project_id")?.toString().trim(); replace({ project_id: projectId || null }); }}>
          <ToolbarField label="Project ID" className="w-40"><Input key={filters.project_id ?? ""} name="project_id" defaultValue={filters.project_id} placeholder="e.g. P001" /></ToolbarField>
          <Button type="submit" variant="outline" className="mt-[1.375rem]">Apply</Button>
        </form>
        {filters.q || filters.action_type || filters.project_id ? <Button variant="ghost" onClick={() => router.replace(pathname)}><X className="size-4" />Clear</Button> : null}
      </PageToolbar>

      {query.isPending ? <LoadingPanel label="Loading activity log" /> : null}
      {query.isError ? <ErrorPanel description={query.error instanceof Error ? query.error.message : "Activity could not be loaded."} onRetry={() => void query.refetch()} /> : null}
      {data ? (
        <section className="overflow-hidden rounded-xl border bg-card shadow-xs" aria-label="Activity log">
          <DataTable items={data.items ?? []} columns={columns} getRowKey={(item) => item.id} caption="Operational activity log" mobileRenderer={(item) => <ActivityMobileItem item={item} />} empty={<StatePanel title="No matching activity" description="No events match the current search and filters." icon={ScrollText} className="m-4" />} />
          <Pagination page={data.page} pageSize={data.page_size} total={data.total} onPageChange={(page) => replace({ page })} />
        </section>
      ) : null}
    </div>
  );
}

const columns: DataTableColumn<UiActivityItem>[] = [
  { id: "time", header: "Time", cell: (item) => <time className="whitespace-nowrap font-mono text-xs text-muted-foreground" dateTime={item.created_at}>{formatTimestamp(item.created_at)}</time> },
  { id: "action", header: "Action", cell: (item) => <StatusBadge value={item.action_type} showDot={false} /> },
  { id: "summary", header: "Summary", className: "min-w-80", cell: (item) => <div><p className="text-sm leading-5">{item.summary}</p>{item.object_ref ? <p className="mt-1 font-mono text-xs text-muted-foreground">{item.object_ref}</p> : null}</div> },
  { id: "project", header: "Project", cell: (item) => item.project_id ? <Link href={`/projects/${item.project_id}`} className="inline-flex min-h-11 items-center gap-1 font-mono text-xs text-primary hover:underline">{item.project_id}<ExternalLink className="size-3" /></Link> : <span className="text-xs text-muted-foreground">Workspace</span> },
  { id: "actor", header: "Actor", cell: (item) => <div className="text-xs"><p>{item.actor || "System"}</p><p className="mt-1 text-muted-foreground">{item.surface || "internal"}</p></div> },
];

function ActivityMobileItem({ item }: { item: UiActivityItem }) {
  return <ResponsiveListItem title={item.summary} metadata={`${formatTimestamp(item.created_at)} · ${item.actor || "System"}`} status={<StatusBadge value={item.action_type} showDot={false} />}>{item.project_id ? <Link href={`/projects/${item.project_id}`} className="inline-flex min-h-11 items-center gap-1 font-mono text-xs text-primary">{item.project_id}<ExternalLink className="size-3" /></Link> : <span className="text-xs">Workspace event</span>}<p className="mt-2 font-mono text-xs">{item.object_ref || item.surface || "No object reference"}</p></ResponsiveListItem>;
}
