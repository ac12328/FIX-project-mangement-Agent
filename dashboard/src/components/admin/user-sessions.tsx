"use client";

import { RefreshCw } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import type { AuthSessionRecord } from "@/lib/api";
import { cn } from "@/lib/utils";

function formatDate(value: string | null): string {
  if (!value) return "Never";
  const date = new Date(value.endsWith("Z") ? value : `${value}Z`);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

export function UserSessions({
  sessions,
  loading,
  busyAction,
  onRefresh,
  onRevoke,
}: {
  sessions: AuthSessionRecord[];
  loading: boolean;
  busyAction: string | null;
  onRefresh: () => void;
  onRevoke: (sessionId: string) => void;
}) {
  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold">Browser sessions</h3>
          <p className="text-xs text-muted-foreground">Active and recently terminated sessions are shown.</p>
        </div>
        <Button size="sm" variant="ghost" onClick={onRefresh} disabled={loading}>
          <RefreshCw className={cn("size-4", loading && "animate-spin")} />Refresh
        </Button>
      </div>
      {loading && sessions.length === 0 ? (
        <p className="py-4 text-sm text-muted-foreground" role="status">Loading sessions...</p>
      ) : sessions.length === 0 ? (
        <p className="rounded-lg border border-dashed p-4 text-sm text-muted-foreground">No sessions recorded for this user.</p>
      ) : (
        <div className="grid gap-2 lg:grid-cols-2">
          {sessions.map((row) => (
            <div key={row.session_id} className="rounded-lg border bg-background p-3">
              <div className="flex items-start justify-between gap-3">
                <div className="flex flex-wrap items-center gap-1.5">
                  <Badge variant={row.active ? "secondary" : "outline"}>{row.active ? "Active" : "Terminated"}</Badge>
                  <Badge variant="outline">{row.auth_method}</Badge>
                  {row.current ? <Badge>Current</Badge> : null}
                </div>
                {row.active ? (
                  <Button
                    size="xs"
                    variant="destructive"
                    onClick={() => onRevoke(row.session_id)}
                    disabled={busyAction === `session:${row.session_id}`}
                  >
                    Revoke
                  </Button>
                ) : null}
              </div>
              <dl className="mt-3 grid gap-1 text-xs text-muted-foreground">
                <div><dt className="inline font-medium text-foreground">Last seen: </dt><dd className="inline">{formatDate(row.last_seen_at)}</dd></div>
                <div><dt className="inline font-medium text-foreground">Created: </dt><dd className="inline">{formatDate(row.created_at)}</dd></div>
                <div><dt className="inline font-medium text-foreground">IP: </dt><dd className="inline">{row.ip_address ?? "Unavailable"}</dd></div>
                <div className="truncate" title={row.user_agent ?? "Unavailable"}><dt className="inline font-medium text-foreground">Device: </dt><dd className="inline">{row.user_agent ?? "Unavailable"}</dd></div>
                {!row.active ? <div><dt className="inline font-medium text-foreground">Ended: </dt><dd className="inline">{formatDate(row.revoked_at)} ({row.revoke_reason ?? "expired"})</dd></div> : null}
              </dl>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
