import type { ReactNode } from "react";
import { AlertTriangle } from "lucide-react";

import { LoadingPanel, StatePanel } from "@/components/state-panel";
import { formatTimestamp } from "@/lib/ui-format";

type SectionQuery = {
  data: unknown;
  isPending: boolean;
  isError: boolean;
  isFetching: boolean;
  dataUpdatedAt: number;
  refetch: () => Promise<unknown>;
};

/** Keep successful data visible while a section refreshes or recovers. */
export function QuerySection({ label, query, children }: {
  label: string;
  query: SectionQuery;
  children: ReactNode;
}) {
  const hasData = query.data !== undefined;
  if (!query.isPending && !query.isError && !query.isFetching && (children == null || children === false)) return null;
  return (
    <section aria-label={label} aria-busy={query.isFetching} className="space-y-3">
      {query.isPending ? <LoadingPanel label={`Loading ${label.toLowerCase()}`} /> : null}
      {query.isError ? (
        <div role="alert">
          <StatePanel
            icon={AlertTriangle}
            title={`${label} could not be ${hasData ? "refreshed" : "loaded"}`}
            description={hasData
              ? `Showing previously retrieved data (last loaded ${formatTimestamp(new Date(query.dataUpdatedAt).toISOString())}). It may be out of date.`
              : "We couldn't retrieve this section. Retry without leaving the page."}
            tone="danger"
            actionLabel={query.isFetching ? "Retrying?" : "Retry"}
            onAction={() => { void query.refetch(); }}
            actionDisabled={query.isFetching}
          />
        </div>
      ) : null}
      {hasData && query.isFetching ? <p role="status" className="text-xs text-muted-foreground">Refreshing {label.toLowerCase()}?</p> : null}
      {hasData ? children : null}
    </section>
  );
}
