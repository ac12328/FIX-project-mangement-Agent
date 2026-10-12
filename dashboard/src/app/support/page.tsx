"use client";

import { Suspense } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { ArrowRight, Search, Ticket, X } from "lucide-react";

import { DataTable, type DataTableColumn } from "@/components/data-table";
import { PageHeader } from "@/components/page-header";
import { PageToolbar, ToolbarField } from "@/components/page-toolbar";
import { Pagination } from "@/components/pagination";
import { ResponsiveListItem } from "@/components/responsive-list";
import { ErrorPanel, LoadingPanel, StatePanel } from "@/components/state-panel";
import { StatusBadge } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { notifyError, notifySuccess } from "@/lib/notifications";
import { postSupportAction } from "@/lib/support-api";
import { formatAge, formatTimestamp } from "@/lib/ui-format";
import { getUiSupport, uiV2Keys } from "@/lib/ui-v2-api";
import type { DeskSupportItem, UiSupportFilters } from "@/lib/ui-v2-types";
import { pageFromSearchParam, updateSearchParams } from "@/lib/url-state";
import { cn } from "@/lib/utils";

const PAGE_SIZE = 25;

const QUEUE_TABS = [
  { id: "needs_me", label: "Needs us" },
  { id: "waiting", label: "Waiting" },
  { id: "stale", label: "Stale" },
  { id: "on_project", label: "On project" },
  { id: "noise", label: "Noise" },
  { id: "all", label: "All" },
] as const;

const NEXT_LABELS: Record<string, string> = {
  reply: "Reply",
  follow_up: "Follow up",
  close_stale: "Close",
  link_project: "Link project",
  review: "Review",
};

const TYPE_LABELS: Record<string, string> = {
  incident: "Incident",
  trade: "Trade",
  access: "Access",
  project: "Project",
  other: "Other",
  noise: "Noise",
};

export default function SupportPage() {
  return (
    <Suspense fallback={<LoadingPanel label="Loading desk issues" />}>
      <SupportContent />
    </Suspense>
  );
}

function SupportContent() {
  const pathname = usePathname();
  const router = useRouter();
  const searchParams = useSearchParams();
  const queryClient = useQueryClient();
  const queue = searchParams.get("queue") || "needs_me";
  const filters: UiSupportFilters = {
    q: searchParams.get("q") || undefined,
    queue,
    unidentified_only: searchParams.get("unidentified") === "1" || undefined,
    sort: (searchParams.get("sort") || "priority") as NonNullable<UiSupportFilters["sort"]>,
    page: pageFromSearchParam(searchParams.get("page")),
    page_size: PAGE_SIZE,
  };
  const query = useQuery({ queryKey: uiV2Keys.support(filters), queryFn: () => getUiSupport(filters) });
  const closeTicket = useMutation({
    mutationFn: ({ ticketId, action }: { ticketId: string; action: string }) =>
      postSupportAction(ticketId, action),
    onSuccess: () => {
      notifySuccess("Ticket updated");
      void queryClient.invalidateQueries({ queryKey: ["ui-v2", "support"] });
    },
    onError: (error) => notifyError(error, "The ticket could not be updated."),
  });

  function replace(updates: Record<string, string | number | null | undefined>) {
    const next = updateSearchParams(searchParams, { ...updates, page: updates.page ?? null });
    router.replace(`${pathname}${next ? `?${next}` : ""}`, { scroll: false });
  }

  const data = query.data;
  const counts = data?.queue_counts ?? {};

  return (
    <div className="mx-auto max-w-[1500px] space-y-6">
      <PageHeader
        eyebrow="Work"
        title="Desk issues"
        description="What needs us, who is waiting, and what can be closed. One row should be enough to decide."
        metadata={data ? <>{counts.all_active ?? data.total} active <span aria-hidden="true">•</span> Updated {formatTimestamp(data.generated_at)}</> : undefined}
      />

      <div className="flex flex-wrap gap-2" role="tablist" aria-label="Desk queues">
        {QUEUE_TABS.map((tab) => {
          const count = tab.id === "all" ? counts.all_active : counts[tab.id];
          const active = queue === tab.id;
          return (
            <button
              key={tab.id}
              type="button"
              role="tab"
              aria-selected={active}
              onClick={() => replace({ queue: tab.id === "needs_me" ? null : tab.id })}
              className={cn(
                "inline-flex cursor-pointer items-center gap-2 rounded-full border px-3 py-1.5 text-sm transition-colors",
                active
                  ? "border-primary bg-primary/10 font-semibold text-primary"
                  : "border-border bg-card text-muted-foreground hover:border-primary/40 hover:text-foreground",
              )}
            >
              {tab.label}
              <span className="font-mono text-xs">{count ?? 0}</span>
            </button>
          );
        })}
      </div>

      <PageToolbar summary={data ? `${data.total} in this queue` : "Loading ticket count"}>
        <form
          className="flex min-w-0 flex-1 gap-2 sm:max-w-md"
          onSubmit={(event) => {
            event.preventDefault();
            const q = new FormData(event.currentTarget).get("q")?.toString().trim();
            replace({ q: q || null });
          }}
        >
          <ToolbarField label="Search" className="min-w-0 flex-1">
            <Input key={filters.q ?? ""} name="q" defaultValue={filters.q} placeholder="Ticket, client, account, or subject" />
          </ToolbarField>
          <Button type="submit" variant="outline" className="mt-[1.375rem]" aria-label="Search tickets">
            <Search className="size-4" />
          </Button>
        </form>
        <label className="flex cursor-pointer items-center gap-2 pb-2 text-sm text-muted-foreground">
          <input
            type="checkbox"
            className="size-4 accent-primary"
            checked={Boolean(filters.unidentified_only)}
            onChange={(event) => replace({ unidentified: event.target.checked ? "1" : null })}
          />
          Unidentified only
        </label>
        {filters.q || filters.unidentified_only || queue !== "needs_me" ? (
          <Button variant="ghost" onClick={() => router.replace(pathname)}>
            <X className="size-4" />Clear
          </Button>
        ) : null}
      </PageToolbar>

      {query.isPending ? <LoadingPanel label="Loading desk issues" /> : null}
      {query.isError ? (
        <ErrorPanel
          description={query.error instanceof Error ? query.error.message : "Desk issues could not be loaded."}
          onRetry={() => void query.refetch()}
        />
      ) : null}
      {data ? (
        <section className="overflow-hidden rounded-xl border bg-card shadow-xs" aria-label="Desk issues">
          <DataTable
            items={data.items ?? []}
            columns={columns(closeTicket.mutate, closeTicket.isPending)}
            getRowKey={(item) => item.ticket_id}
            caption="Desk issue worklist"
            mobileRenderer={(item) => (
              <TicketMobileItem
                item={item}
                onClose={(action) => closeTicket.mutate({ ticketId: item.ticket_id, action })}
                closing={closeTicket.isPending}
              />
            )}
            empty={<StatePanel title="Nothing in this queue" description="No desk issues match the selected queue." icon={Ticket} className="m-4" />}
          />
          <Pagination page={data.page} pageSize={data.page_size} total={data.total} onPageChange={(page) => replace({ page })} />
        </section>
      ) : null}
    </div>
  );
}

function columns(
  onClose: (input: { ticketId: string; action: string }) => void,
  closing: boolean,
): DataTableColumn<DeskSupportItem>[] {
  return [
    {
      id: "who",
      header: "Who",
      className: "min-w-44",
      cell: (item) => (
        <div>
          <p className="font-medium">{item.party_name || "Unidentified"}</p>
          {item.account_hint ? <p className="mt-1 font-mono text-xs text-muted-foreground">{item.account_hint}</p> : null}
        </div>
      ),
    },
    {
      id: "issue",
      header: "Issue",
      className: "min-w-80",
      cell: (item) => (
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <Link href={`/support/${item.ticket_id}`} className="font-medium text-primary hover:underline">
              {item.display_title || item.title}
            </Link>
            <TypeChip value={item.case_type} />
            {item.mixed_thread ? <StatusBadge value="yellow" label="Mixed thread" showDot={false} /> : null}
            {item.already_answered ? <StatusBadge value="green" label="Already answered" showDot={false} /> : null}
          </div>
          <p className="mt-1 font-mono text-xs text-muted-foreground">{item.ticket_id}</p>
          {item.last_preview ? <p className="mt-1 line-clamp-2 text-xs text-muted-foreground">{item.last_preview}</p> : null}
        </div>
      ),
    },
    {
      id: "next",
      header: "Next",
      cell: (item) => (
        <div className="space-y-2">
          <StatusBadge value={item.next_action === "close_stale" ? "green" : "blue"} label={NEXT_LABELS[item.next_action] || item.next_action} showDot={false} />
          {item.can_execute && (item.next_action === "close_stale" || item.queue === "stale" || item.queue === "noise") ? (
            <div className="flex flex-wrap gap-1">
              <Button size="sm" variant="outline" disabled={closing} onClick={() => onClose({ ticketId: item.ticket_id, action: item.queue === "noise" ? "not_an_issue" : "close_stale" })}>
                {item.queue === "noise" ? "Not an issue" : "Close"}
              </Button>
            </div>
          ) : null}
        </div>
      ),
    },
    {
      id: "age",
      header: "Age",
      cell: (item) => (
        <div>
          <p className="text-sm">{formatAge(item.age_days)}</p>
          <p className="mt-1 text-xs text-muted-foreground">{item.ball_with === "External" ? "With client" : "With us"}</p>
        </div>
      ),
    },
    {
      id: "project",
      header: "Project",
      cell: (item) =>
        item.related_project_id && item.on_project ? (
          <Link href={`/projects/${item.related_project_id}`} className="text-sm text-primary hover:underline">
            {item.related_project_name || item.related_project_id}
          </Link>
        ) : (
          <span className="text-xs text-muted-foreground">Unlinked</span>
        ),
    },
    {
      id: "open",
      header: <span className="sr-only">Open</span>,
      className: "text-right",
      cell: (item) => (
        <Button size="icon-sm" variant="ghost" render={<Link href={`/support/${item.ticket_id}`} aria-label={`Open ${item.ticket_id}`} />}>
          <ArrowRight className="size-4" />
        </Button>
      ),
    },
  ];
}

function TypeChip({ value }: { value: string }) {
  return <StatusBadge value={value === "incident" ? "red" : value === "trade" ? "yellow" : "blue"} label={TYPE_LABELS[value] || value} showDot={false} />;
}

function TicketMobileItem({
  item,
  onClose,
  closing,
}: {
  item: DeskSupportItem;
  onClose: (action: string) => void;
  closing: boolean;
}) {
  return (
    <ResponsiveListItem
      title={<Link href={`/support/${item.ticket_id}`} className="text-primary">{item.display_title || item.title}</Link>}
      metadata={`${item.ticket_id} · ${item.party_name || "Unidentified"}`}
      status={<TypeChip value={item.case_type} />}
      action={
        <Button size="sm" variant="outline" render={<Link href={`/support/${item.ticket_id}`} />}>
          Open <ArrowRight className="size-4" />
        </Button>
      }
    >
      <p className="mt-2 text-sm text-muted-foreground">{item.last_preview || "No recent message"}</p>
      <div className="mt-3 flex flex-wrap gap-2">
        <StatusBadge value={item.next_action === "close_stale" ? "green" : "blue"} label={NEXT_LABELS[item.next_action] || item.next_action} showDot={false} />
        <StatusBadge value="gray" label={formatAge(item.age_days)} showDot={false} />
      </div>
      {item.can_execute && (item.next_action === "close_stale" || item.queue === "stale" || item.queue === "noise") ? (
        <Button className="mt-3" size="sm" variant="outline" disabled={closing} onClick={() => onClose(item.queue === "noise" ? "not_an_issue" : "close_stale")}>
          {item.queue === "noise" ? "Not an issue" : "Close"}
        </Button>
      ) : null}
    </ResponsiveListItem>
  );
}
