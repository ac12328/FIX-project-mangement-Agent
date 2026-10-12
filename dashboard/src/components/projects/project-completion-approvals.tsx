"use client";

import { useCallback, useEffect, useState } from "react";

import { ErrorPanel, LoadingPanel } from "@/components/state-panel";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { StatusBadge } from "@/components/status-badge";
import {
  approveIntent,
  getApprovals,
  rejectIntent,
  type ApprovalIntent,
} from "@/lib/api";
import { notifyError, notifySuccess } from "@/lib/notifications";

const PROJECT_COMPLETION_ACTION = "project_completion";

export function ProjectCompletionApprovals({
  projectId,
}: {
  projectId: string;
}) {
  const [items, setItems] = useState<ApprovalIntent[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const all = await getApprovals("pending");
      setItems(
        all.filter(
          (item) =>
            item.action_type === PROJECT_COMPLETION_ACTION
            && item.target_id === projectId,
        ),
      );
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load completion approvals.");
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => { void load(); }, [load]);

  async function act(item: ApprovalIntent, action: "approve" | "reject") {
    setBusy(item.action_id);
    try {
      if (action === "approve") await approveIntent(item.action_id, true);
      else await rejectIntent(item.action_id);
      notifySuccess(action === "approve" ? "Project completed" : "Completion rejected");
      await load();
    } catch (err) {
      notifyError(err, "Completion action failed.");
    } finally {
      setBusy("");
    }
  }

  if (loading && !items.length) return <LoadingPanel label="Loading completion approvals" />;
  if (error) return <ErrorPanel description={error} onRetry={() => void load()} />;
  if (!items.length) return null;

  return (
    <section className="space-y-3" aria-label="Completion approvals">
      {items.map((item) => (
        <Card key={item.action_id} className="border border-warning/30 bg-warning/5">
          <CardHeader className="pb-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <CardTitle className="text-base">Confirm project completion</CardTitle>
              <StatusBadge value={item.status} />
            </div>
            <p className="mt-1 text-sm text-muted-foreground">
              This sets the project to Completed. An Admin can confirm it directly.
            </p>
          </CardHeader>
          <CardContent className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
            <Button
              variant="outline"
              disabled={busy === item.action_id}
              onClick={() => void act(item, "reject")}
            >
              Reject
            </Button>
            <Button
              disabled={busy === item.action_id}
              onClick={() => void act(item, "approve")}
            >
              {busy === item.action_id ? "Completing…" : "Confirm completion"}
            </Button>
          </CardContent>
        </Card>
      ))}
    </section>
  );
}
