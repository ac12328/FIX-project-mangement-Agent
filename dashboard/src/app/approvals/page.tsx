"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { ArrowRight, CheckCircle2, ClipboardCheck, ShieldCheck, TriangleAlert } from "lucide-react";

import { PageHeader } from "@/components/page-header";
import { ErrorPanel, LoadingPanel, StatePanel } from "@/components/state-panel";
import { StatusBadge } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  approveIntent,
  getApprovals,
  rejectIntent,
  type ApprovalIntent,
} from "@/lib/api";
import { notifyError, notifySuccess, notifyWarning } from "@/lib/notifications";

const PROJECT_COMPLETION_ACTION = "project_completion";

function payloadText(
  payload: Record<string, unknown>,
  key: string,
  fallback = "Not recorded",
): string {
  const value = payload[key];
  return typeof value === "string" && value.trim() ? value : fallback;
}

function humanizeAction(value: string): string {
  return value.replaceAll("_", " ");
}

function notifyEmailApproval(intent: ApprovalIntent) {
  switch (intent.status) {
    case "executed":
      notifySuccess("Email sent", "Delivery was confirmed.");
      break;
    case "uncertain":
      notifyWarning(
        "Email delivery unconfirmed",
        "Provider acceptance is unknown. Reconcile manually with the email provider and outbox before any further action. Do not resend while delivery is uncertain.",
      );
      break;
    case "failed":
      notifyError(new Error("Email send failed. Check the approval record for details."));
      break;
    case "queued":
    case "executing":
      notifyWarning("Email queued, not confirmed sent", "Check the approval record for the delivery outcome.");
      break;
    case "approved":
      notifyWarning("Email approved, not sent", "Check the approval record for the delivery outcome.");
      break;
    default:
      notifyWarning("Email delivery not confirmed", `Approval status: ${intent.status}. Check the approval record before taking further action.`);
  }
}

function ProjectCompletionSummary({ item }: { item: ApprovalIntent }) {
  const projectId = payloadText(item.payload, "project_id", item.target_id);
  const clientName = payloadText(item.payload, "client_name");
  const currentStatus = payloadText(item.payload, "current_status");
  const currentMilestone = payloadText(item.payload, "current_milestone");

  return (
    <div className="space-y-4">
      <dl className="grid gap-3 sm:grid-cols-3">
        <div className="rounded-lg border bg-muted/30 p-3">
          <dt className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Client</dt>
          <dd className="mt-1 text-sm font-medium text-foreground">{clientName}</dd>
          <dd className="mt-1 font-mono text-xs text-muted-foreground">{projectId}</dd>
        </div>
        <div className="rounded-lg border bg-muted/30 p-3">
          <dt className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Current status</dt>
          <dd className="mt-1 text-sm font-medium text-foreground">{currentStatus}</dd>
        </div>
        <div className="rounded-lg border bg-muted/30 p-3">
          <dt className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Milestone</dt>
          <dd className="mt-1 text-sm font-medium text-foreground">{currentMilestone}</dd>
        </div>
      </dl>

      <div className="flex gap-3 rounded-lg border border-primary/20 bg-primary/5 p-4">
        <CheckCircle2 className="mt-0.5 size-5 shrink-0 text-primary" aria-hidden="true" />
        <div>
          <p className="text-sm font-medium text-foreground">Completion effect</p>
          <p className="mt-1 text-sm leading-6 text-muted-foreground">
            Confirming sets the project status to Completed, assigns the ball to Internal,
            and clears the next action.
          </p>
        </div>
      </div>
    </div>
  );
}

export default function ApprovalsPage() {
  const [items, setItems] = useState<ApprovalIntent[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setItems(await getApprovals());
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load approvals.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  async function act(item: ApprovalIntent, action: "approve" | "reject") {
    const isProjectCompletion = item.action_type === PROJECT_COMPLETION_ACTION;
    setBusy(item.action_id);
    try {
      const intent = action === "approve"
        ? await approveIntent(
            item.action_id,
            isProjectCompletion
              || item.action_type === "email_send"
              || item.action_type.startsWith("calendar_"),
          )
        : await rejectIntent(item.action_id);
      if (action === "approve" && item.action_type === "email_send") {
        notifyEmailApproval(intent);
      } else {
        notifySuccess(
          action === "approve"
            ? isProjectCompletion ? "Project completed" : "Action approved"
            : "Action rejected",
          isProjectCompletion
            ? payloadText(item.payload, "client_name", item.target_id)
            : humanizeAction(item.action_type),
        );
      }
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Approval action failed.");
      notifyError(err, "Approval action failed.");
    } finally {
      setBusy("");
    }
  }

  const pendingCount = items.filter((item) => item.status === "pending").length;

  return (
    <div className="mx-auto max-w-[1400px] space-y-6">
      <PageHeader
        eyebrow="Work"
        title="Approvals"
        description="High-risk actions that need a second person. Project completions also appear on the project page."
        metadata={!loading || items.length ? <>{pendingCount} pending <span aria-hidden="true">•</span> {items.length} total</> : undefined}
      />

      <Card className="border-dashed bg-muted/20 shadow-none">
        <CardContent className="flex flex-col gap-4 py-4 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex min-w-0 gap-3">
            <ClipboardCheck className="mt-0.5 size-5 shrink-0 text-primary" aria-hidden="true" />
            <div>
              <p className="text-sm font-medium text-foreground">Looking for a project or email update?</p>
              <p className="mt-1 text-sm leading-6 text-muted-foreground">
                Project-truth proposals are listed under Pending reviews, not Approvals.
              </p>
            </div>
          </div>
          <Button variant="outline" render={<Link href="/reviews" />}>
            Open pending reviews
            <ArrowRight className="size-4" aria-hidden="true" />
          </Button>
        </CardContent>
      </Card>

      {error ? <ErrorPanel description={error} onRetry={() => void load()} /> : null}
      {loading && !items.length ? <LoadingPanel label="Loading approvals" /> : null}

      {!loading && !error && !items.length ? (
        <StatePanel
          icon={ShieldCheck}
          title="No approval history"
          description="Actions that require operator confirmation will appear here."
        />
      ) : null}

      {items.length ? (
        <section className="space-y-4" aria-label="Approval requests">
          {items.map((item) => {
            const isProjectCompletion = item.action_type === PROJECT_COMPLETION_ACTION;
            return (
              <Card key={item.action_id} className="border bg-card shadow-xs">
                <CardHeader className="border-b pb-4">
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="min-w-0">
                      <CardTitle className="text-base capitalize">
                        {isProjectCompletion ? "Complete project" : humanizeAction(item.action_type)}
                      </CardTitle>
                      <p className="mt-1 text-xs text-muted-foreground">
                        {item.target_type}: <span className="font-mono text-foreground">{item.target_id}</span>
                      </p>
                    </div>
                    <div className="flex flex-wrap gap-2">
                      <StatusBadge value={item.risk_class} label={`${item.risk_class} risk`} />
                      <StatusBadge value={item.status} />
                    </div>
                  </div>
                </CardHeader>
                <CardContent className="space-y-4 pt-0">
                  {isProjectCompletion ? (
                    <ProjectCompletionSummary item={item} />
                  ) : (
                    <pre className="max-h-72 overflow-auto rounded-lg border bg-muted/40 p-4 font-mono text-xs leading-5 whitespace-pre-wrap">
                      {JSON.stringify(item.payload, null, 2)}
                    </pre>
                  )}
                  {item.action_type === "email_send" && item.status === "uncertain" ? (
                    <div role="alert" className="flex gap-3 rounded-lg border border-warning/30 bg-warning/10 p-4 text-sm leading-6 text-foreground">
                      <TriangleAlert className="mt-0.5 size-5 shrink-0 text-warning" aria-hidden="true" />
                      <div>
                        <p className="font-medium">Email delivery unconfirmed</p>
                        <p className="mt-1">
                          The email may have been sent, but provider acceptance is unknown. Manually reconcile with the email provider and outbox before taking further action. Do not resend while delivery is uncertain.
                        </p>
                      </div>
                    </div>
                  ) : null}
                  {item.status === "pending" ? (
                    <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
                      <Button
                        variant="outline"
                        disabled={busy === item.action_id}
                        onClick={() => void act(item, "reject")}
                      >
                        {busy === item.action_id ? "Working…" : "Reject"}
                      </Button>
                      <Button
                        disabled={busy === item.action_id}
                        onClick={() => void act(item, "approve")}
                      >
                        {busy === item.action_id
                          ? isProjectCompletion ? "Completing…" : "Working…"
                          : isProjectCompletion
                            ? "Confirm completion"
                            : `Approve${item.action_type === "email_send" ? " & Send" : item.action_type.startsWith("calendar_") ? " & Execute" : ""}`}
                      </Button>
                    </div>
                  ) : null}
                </CardContent>
              </Card>
            );
          })}
        </section>
      ) : null}
    </div>
  );
}
