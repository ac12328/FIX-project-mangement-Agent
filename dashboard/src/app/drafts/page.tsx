"use client";

import { Suspense, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { FileEdit, MailPlus, Search, Send, Trash2, X } from "lucide-react";

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
import { approveAndSendDraft, bulkDraftAction, draftAction } from "@/lib/api";
import { notifyError, notifySuccess } from "@/lib/notifications";
import { formatTimestamp } from "@/lib/ui-format";
import { getUiDrafts, uiV2Keys } from "@/lib/ui-v2-api";
import type { UiDraftFilters, UiDraftItem } from "@/lib/ui-v2-types";
import { pageFromSearchParam, updateSearchParams } from "@/lib/url-state";

const PAGE_SIZE = 25;

export default function DraftsPage() {
  return <Suspense fallback={<LoadingPanel label="Loading draft queue" />}><DraftsContent /></Suspense>;
}

function DraftsContent() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [busyId, setBusyId] = useState<string | null>(null);
  const [bulkBusy, setBulkBusy] = useState(false);
  const filters: UiDraftFilters = {
    q: searchParams.get("q") || undefined,
    status: searchParams.get("status") || undefined,
    page: pageFromSearchParam(searchParams.get("page")),
    page_size: PAGE_SIZE,
  };
  const query = useQuery({ queryKey: uiV2Keys.drafts(filters), queryFn: () => getUiDrafts(filters) });
  const data = query.data;
  const visibleIds = (data?.items ?? []).map((draft) => draft.draft_id);
  const selectedVisible = visibleIds.filter((id) => selected.has(id));
  const allVisibleSelected = visibleIds.length > 0 && selectedVisible.length === visibleIds.length;

  function replace(updates: Record<string, string | number | null | undefined>) {
    const next = updateSearchParams(searchParams, { ...updates, page: updates.page ?? null });
    router.replace(`${pathname}${next ? `?${next}` : ""}`, { scroll: false });
  }

  function toggleSelected(id: string) {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function toggleAllVisible() {
    setSelected((current) => {
      const next = new Set(current);
      for (const id of visibleIds) {
        if (allVisibleSelected) next.delete(id);
        else next.add(id);
      }
      return next;
    });
  }

  async function runAction(draftId: string, action: "approve" | "discard") {
    setBusyId(draftId);
    try {
      const result = action === "approve"
        ? await approveAndSendDraft(draftId)
        : await draftAction(draftId, action);
      if (!result.success) throw new Error(result.message);
      notifySuccess(action === "approve" ? "Draft sent" : "Draft discarded", result.message);
      setSelected((current) => {
        const next = new Set(current);
        next.delete(draftId);
        return next;
      });
      await query.refetch();
    } catch (error) {
      notifyError(error, "The draft action could not be completed.");
    } finally {
      setBusyId(null);
    }
  }

  async function runBulkAction(action: "approve" | "discard") {
    if (selectedVisible.length === 0) return;
    setBulkBusy(true);
    try {
      const result = await bulkDraftAction(selectedVisible, action);
      const succeeded = result.results.filter((item) => item.success).length;
      const failed = result.results.length - succeeded;
      if (failed) {
        notifyError(new Error(`${succeeded} succeeded and ${failed} failed.`));
      } else {
        notifySuccess(
          action === "approve" ? "Drafts sent" : "Drafts discarded",
          `${succeeded} draft${succeeded === 1 ? "" : "s"} processed successfully.`,
        );
      }
      setSelected(new Set());
      await query.refetch();
    } catch (error) {
      notifyError(error, "The bulk draft action could not be completed.");
    } finally {
      setBulkBusy(false);
    }
  }

  const columns: DataTableColumn<UiDraftItem>[] = [
    {
      id: "select",
      header: (
        <label className="flex size-11 cursor-pointer items-center justify-center" aria-label="Select all drafts on this page">
          <input type="checkbox" checked={allVisibleSelected} onChange={toggleAllVisible} className="size-4 accent-primary" />
        </label>
      ),
      className: "w-12 px-0",
      cell: (draft) => (
        <label className="flex size-11 cursor-pointer items-center justify-center" aria-label={`Select draft ${draft.draft_id}`}>
          <input type="checkbox" checked={selected.has(draft.draft_id)} onChange={() => toggleSelected(draft.draft_id)} className="size-4 accent-primary" />
        </label>
      ),
    },
    {
      id: "draft",
      header: "Draft",
      className: "min-w-52",
      cell: (draft) => (
        <div>
          <Link href={`/drafts/${draft.draft_id}`} className="font-mono text-sm font-semibold text-primary hover:underline">
            {draft.subject || "Untitled draft"}
          </Link>
          <p className="mt-1 text-xs text-muted-foreground">
            {draft.to_email ? `To ${draft.to_email}` : "Recipient not set"}
          </p>
        </div>
      ),
    },
    {
      id: "context",
      header: "Context",
      cell: (draft) => (
        <div className="space-y-1 text-xs">
          <p>{draft.task_type || "reply"}</p>
          <p className="font-mono text-muted-foreground">{draft.project_id || draft.ticket_id || "Standalone"}</p>
        </div>
      ),
    },
    { id: "status", header: "Status", cell: (draft) => <StatusBadge value={draft.status} /> },
    { id: "version", header: "Version", cell: (draft) => <span className="font-mono text-xs">v{draft.draft_version}</span> },
    { id: "created", header: "Created", cell: (draft) => <span className="text-xs text-muted-foreground">{formatTimestamp(draft.created_at)}</span> },
    {
      id: "actions",
      header: <span className="sr-only">Actions</span>,
      className: "min-w-64 text-right",
      cell: (draft) => (
        <div className="flex justify-end gap-2">
          <Button size="sm" variant="outline" render={<Link href={`/drafts/${draft.draft_id}`} />} disabled={busyId === draft.draft_id}>
            <FileEdit className="size-4" />Edit
          </Button>
          <Button size="sm" disabled={busyId === draft.draft_id} onClick={() => void runAction(draft.draft_id, "approve")}>
            <Send className="size-4" />{busyId === draft.draft_id ? "Sending..." : "Send"}
          </Button>
          <Button size="icon-sm" variant="destructive" disabled={busyId === draft.draft_id} onClick={() => void runAction(draft.draft_id, "discard")} aria-label={`Discard ${draft.subject || draft.draft_id}`}>
            <Trash2 className="size-4" />
          </Button>
        </div>
      ),
    },
  ];

  return (
    <div className="mx-auto max-w-[1600px] space-y-6">
      <PageHeader
        eyebrow="Work"
        title="Draft queue"
        description="Review recipients and context, edit the message, then send or discard with explicit feedback."
        metadata={data ? <>{data.total} pending drafts <span aria-hidden="true">•</span> Updated {formatTimestamp(data.generated_at)}</> : undefined}
        actions={<Button render={<Link href="/mail?tab=compose" />}><MailPlus className="size-4" />Compose email</Button>}
      />

      <PageToolbar summary={selectedVisible.length ? `${selectedVisible.length} selected on this page` : data ? `${data.total} matching drafts` : "Loading draft count"}>
        <form
          className="flex min-w-0 flex-1 gap-2 sm:max-w-md"
          onSubmit={(event) => {
            event.preventDefault();
            const value = new FormData(event.currentTarget).get("q")?.toString().trim();
            replace({ q: value || null });
          }}
        >
          <ToolbarField label="Search drafts" className="min-w-0 flex-1">
            <Input key={filters.q ?? ""} name="q" defaultValue={filters.q} placeholder="Subject, recipient, project, or content" />
          </ToolbarField>
          <Button type="submit" variant="outline" className="mt-[1.375rem]" aria-label="Search drafts"><Search className="size-4" /></Button>
        </form>
        <ToolbarField label="Status" className="sm:w-44">
          <Select value={filters.status ?? "pending"} onValueChange={(value) => replace({ status: value === "pending" ? null : value })}>
            <SelectTrigger><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="pending">Pending queue</SelectItem>
              <SelectItem value="created">Created</SelectItem>
              <SelectItem value="in_review">In review</SelectItem>
              <SelectItem value="send_failed">Send failed</SelectItem>
            </SelectContent>
          </Select>
        </ToolbarField>
        {filters.q || filters.status ? <Button variant="ghost" onClick={() => router.replace(pathname)}><X className="size-4" />Clear</Button> : null}
        {selectedVisible.length ? (
          <div className="flex flex-wrap gap-2 sm:ml-auto">
            <Button variant="outline" disabled={bulkBusy} onClick={() => void runBulkAction("discard")}><Trash2 className="size-4" />Discard selected</Button>
            <Button disabled={bulkBusy} onClick={() => void runBulkAction("approve")}><Send className="size-4" />Send selected</Button>
          </div>
        ) : null}
      </PageToolbar>

      {query.isPending ? <LoadingPanel label="Loading draft queue" /> : null}
      {query.isError ? <ErrorPanel description={query.error instanceof Error ? query.error.message : "Drafts could not be loaded."} onRetry={() => void query.refetch()} /> : null}
      {data ? (
        <section className="overflow-hidden rounded-xl border bg-card shadow-xs" aria-label="Draft queue">
          <DataTable
            items={data.items ?? []}
            columns={columns}
            getRowKey={(draft) => draft.draft_id}
            caption="Pending draft queue"
            mobileRenderer={(draft) => (
              <ResponsiveListItem
                title={<Link href={`/drafts/${draft.draft_id}`} className="text-primary hover:underline">{draft.subject || "Untitled draft"}</Link>}
                metadata={`${draft.to_email || "Recipient not set"} · ${draft.project_id || draft.ticket_id || "Standalone"}`}
                status={<StatusBadge value={draft.status} />}
                action={(
                  <>
                    <label className="flex min-h-11 cursor-pointer items-center gap-2 rounded-lg border px-3 text-sm">
                      <input type="checkbox" checked={selected.has(draft.draft_id)} onChange={() => toggleSelected(draft.draft_id)} className="size-4 accent-primary" />Select
                    </label>
                    <Button size="sm" variant="outline" render={<Link href={`/drafts/${draft.draft_id}`} />}><FileEdit className="size-4" />Edit</Button>
                    <Button size="sm" disabled={busyId === draft.draft_id} onClick={() => void runAction(draft.draft_id, "approve")}><Send className="size-4" />Send</Button>
                    <Button size="sm" variant="destructive" disabled={busyId === draft.draft_id} onClick={() => void runAction(draft.draft_id, "discard")}><Trash2 className="size-4" />Discard</Button>
                  </>
                )}
              >
                <p>{draft.task_type || "Reply"} · v{draft.draft_version} · {formatTimestamp(draft.created_at)}</p>
              </ResponsiveListItem>
            )}
            empty={<StatePanel title="No drafts need attention" description="New generated or composed messages will appear here for review." icon={MailPlus} className="m-4" />}
          />
          <Pagination page={data.page} pageSize={data.page_size} total={data.total} onPageChange={(page) => replace({ page })} />
        </section>
      ) : null}
    </div>
  );
}
