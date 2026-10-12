"use client";

import { Suspense } from "react";
import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { ArrowRight, Building2, Factory, Search, X } from "lucide-react";

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
import { getUiParties, uiV2Keys } from "@/lib/ui-v2-api";
import type { UiParty, UiPartyFilters } from "@/lib/ui-v2-types";
import { pageFromSearchParam, updateSearchParams } from "@/lib/url-state";

const PAGE_SIZE = 25;

type DirectoryKind = "client" | "vendor";

export function PartyDirectoryPage({ kind }: { kind: DirectoryKind }) {
  return <Suspense fallback={<LoadingPanel label={`Loading ${kind}s`} />}><PartyDirectoryContent kind={kind} /></Suspense>;
}

function PartyDirectoryContent({ kind }: { kind: DirectoryKind }) {
  const pathname = usePathname();
  const router = useRouter();
  const searchParams = useSearchParams();
  const filters: UiPartyFilters = {
    org_type: kind,
    q: searchParams.get("q") || undefined,
    sort: (searchParams.get("sort") || "name") as NonNullable<UiPartyFilters["sort"]>,
    page: pageFromSearchParam(searchParams.get("page")),
    page_size: PAGE_SIZE,
  };
  const query = useQuery({ queryKey: uiV2Keys.parties(filters), queryFn: () => getUiParties(filters) });
  const title = kind === "client" ? "Clients" : "Vendors";
  const Icon = kind === "client" ? Building2 : Factory;

  function replace(updates: Record<string, string | number | null | undefined>) {
    const next = updateSearchParams(searchParams, { ...updates, page: updates.page ?? null });
    router.replace(`${pathname}${next ? `?${next}` : ""}`, { scroll: false });
  }

  const data = query.data;
  return (
    <div className="mx-auto max-w-[1400px] space-y-6">
      <PageHeader
        eyebrow="Party graph"
        title={title}
        description={kind === "client" ? "Client organisations, relationship coverage, and connected onboarding projects." : "Cross-project technology and service providers in the operating network."}
        metadata={data ? <>{data.total} organisations <span aria-hidden="true">•</span> Updated {formatTimestamp(data.generated_at)}</> : undefined}
      />
      <PageToolbar summary={data ? `${data.total} matching organisations` : "Loading organisation count"}>
        <form className="flex min-w-0 flex-1 gap-2 sm:max-w-md" onSubmit={(event) => { event.preventDefault(); const q = new FormData(event.currentTarget).get("q")?.toString().trim(); replace({ q: q || null }); }}>
          <ToolbarField label={`Search ${title.toLowerCase()}`} className="min-w-0 flex-1"><Input key={filters.q ?? ""} name="q" defaultValue={filters.q} placeholder="Name, domain, or organisation ID" /></ToolbarField>
          <Button type="submit" variant="outline" className="mt-[1.375rem]" aria-label={`Search ${title.toLowerCase()}`}><Search className="size-4" /></Button>
        </form>
        <ToolbarField label="Sort" className="sm:w-44">
          <Select value={filters.sort} onValueChange={(value) => replace({ sort: value === "name" ? null : value })}>
            <SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectItem value="name">Name</SelectItem><SelectItem value="projects">Project count</SelectItem><SelectItem value="recent">Recently added</SelectItem></SelectContent>
          </Select>
        </ToolbarField>
        {filters.q || filters.sort !== "name" ? <Button variant="ghost" onClick={() => router.replace(pathname)}><X className="size-4" />Clear</Button> : null}
      </PageToolbar>

      {query.isPending ? <LoadingPanel label={`Loading ${title.toLowerCase()}`} /> : null}
      {query.isError ? <ErrorPanel description={query.error instanceof Error ? query.error.message : `${title} could not be loaded.`} onRetry={() => void query.refetch()} /> : null}
      {data ? (
        <section className="overflow-hidden rounded-xl border bg-card shadow-xs" aria-label={title}>
          <DataTable
            items={data.items ?? []}
            columns={partyColumns}
            getRowKey={(party) => party.org_id}
            caption={`${title} directory`}
            mobileRenderer={(party) => <PartyMobileItem party={party} />}
            empty={<StatePanel title={`No ${title.toLowerCase()} found`} description="Try a different search, or wait for the party graph to be populated." icon={Icon} className="m-4" />}
          />
          <Pagination page={data.page} pageSize={data.page_size} total={data.total} onPageChange={(page) => replace({ page })} />
        </section>
      ) : null}
    </div>
  );
}

const partyColumns: DataTableColumn<UiParty>[] = [
  { id: "name", header: "Organisation", className: "min-w-64", cell: (party) => <div><Link href={`/parties/${party.org_id}`} className="font-medium text-primary hover:underline">{party.org_name}</Link><p className="mt-1 font-mono text-xs text-muted-foreground">{party.org_id}</p></div> },
  { id: "type", header: "Type", cell: (party) => <StatusBadge value={party.org_type} showDot={false} /> },
  { id: "domains", header: "Domains", className: "min-w-52", cell: (party) => <p className="line-clamp-2 text-xs text-muted-foreground">{party.domains?.length ? party.domains.join(", ") : "No domains recorded"}</p> },
  { id: "projects", header: "Projects", cell: (party) => <span className="font-mono tabular-nums">{party.project_count}</span> },
  { id: "contacts", header: "Contacts", cell: (party) => <span className="font-mono tabular-nums">{party.contact_count}</span> },
  { id: "open", header: <span className="sr-only">Open</span>, className: "text-right", cell: (party) => <Button size="icon-sm" variant="ghost" render={<Link href={`/parties/${party.org_id}`} aria-label={`Open ${party.org_name}`} />}><ArrowRight className="size-4" /></Button> },
];

function PartyMobileItem({ party }: { party: UiParty }) {
  return <ResponsiveListItem title={<Link href={`/parties/${party.org_id}`} className="text-primary">{party.org_name}</Link>} metadata={`${party.org_id} · ${party.domains?.join(", ") || "No domains"}`} status={<StatusBadge value={party.org_type} showDot={false} />} action={<Button size="sm" variant="outline" render={<Link href={`/parties/${party.org_id}`} />}>Open organisation <ArrowRight className="size-4" /></Button>}><p>{party.project_count} connected projects · {party.contact_count} contacts</p></ResponsiveListItem>;
}
