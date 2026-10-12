"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import {
  AlertTriangle,
  Check,
  CheckCircle2,
  Loader2,
  ShieldAlert,
  ShieldCheck,
  X,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Textarea } from "@/components/ui/textarea";
import {
  cancelOperatorUpdate,
  confirmOperatorUpdate,
  getOperatorUpdate,
  stageOperatorUpdate,
} from "@/lib/assistant-api";
import type { AssistantOperatorUpdatePart } from "@/lib/assistant-types";
import { ApiError, getEvidence } from "@/lib/api";
import type { EvidenceItem } from "@/lib/types";

const OPEN_STATUSES = new Set(["needs_intake", "pending", "executing"]);
const TERMINAL_STATUSES = new Set(["confirmed", "cancelled", "failed", "expired", "superseded"]);

function normalizeCardStatus(value: unknown): string {
  const status = String(value ?? "").trim().toLowerCase();
  if (status === "accepted" || status === "executed" || status === "resolved") return "confirmed";
  if (status === "rejected") return "cancelled";
  return status;
}

function humanizeMilestone(value: unknown): string {
  const text = String(value ?? "").trim();
  if (!text) return "Not set";
  return text.replaceAll("_", " ").replace(/^M(\d+)\s/, "M$1 · ");
}

function errorMessage(caught: unknown): string {
  if (caught instanceof ApiError) return caught.message;
  if (caught instanceof Error) return caught.message;
  return "The operator update could not be applied.";
}

export function OperatorUpdateCard({
  part,
  threadId,
  canManageActions,
  onActivity,
  onResolved,
}: {
  part: AssistantOperatorUpdatePart;
  threadId?: string;
  canManageActions: boolean;
  onActivity?: (message: string) => void;
  onResolved?: () => void;
}) {
  const [status, setStatus] = useState(part.status);
  const [operationId, setOperationId] = useState(part.operation_id);
  const [reason, setReason] = useState(part.reason || "");
  const [evidenceRef, setEvidenceRef] = useState(part.evidence_ref || "");
  const [evidence, setEvidence] = useState<EvidenceItem[]>([]);
  const [pending, setPending] = useState<"confirm" | "cancel" | "intake" | null>(null);
  const [error, setError] = useState<string | null>(part.error || null);
  const [dismissed, setDismissed] = useState(false);
  const fields = part.fields || {};
  const currentValues = part.current_values || {};
  const fromMilestone = humanizeMilestone(currentValues.current_milestone);
  const toMilestone = humanizeMilestone(fields.current_milestone);
  const override = (part.kind || "operator_override") !== "evaluator_aligned";
  const resolved = !OPEN_STATUSES.has(String(status).toLowerCase());

  useEffect(() => {
    if (!part.project_id) return;
    void getEvidence(part.project_id)
      .then(setEvidence)
      .catch(() => setEvidence([]));
  }, [part.project_id]);

  useEffect(() => {
    if (!part.project_id || !operationId) return;
    if (status === "needs_intake" || status === "executing") return;
    if (TERMINAL_STATUSES.has(normalizeCardStatus(status))) return;
    let cancelled = false;
    void getOperatorUpdate(part.project_id, operationId)
      .then((card) => {
        if (cancelled) return;
        const next = normalizeCardStatus(card.status);
        setStatus((current) => {
          const local = normalizeCardStatus(current);
          if (TERMINAL_STATUSES.has(local) && !TERMINAL_STATUSES.has(next)) return current;
          if (local === "executing" && next === "pending") return current;
          return next || current;
        });
        if (card.part?.error) setError(card.part.error);
      })
      .catch((caught) => {
        if (cancelled) return;
        if (caught instanceof ApiError && caught.errorCode === "pending_operation_not_found") {
          setStatus((current) => (
            TERMINAL_STATUSES.has(normalizeCardStatus(current)) ? current : "expired"
          ));
        }
      });
    return () => {
      cancelled = true;
    };
  }, [operationId, part.project_id, status]);

  if (dismissed) return null;

  async function submitIntake() {
    if (!threadId) {
      setError("This conversation is missing a thread id.");
      return;
    }
    setPending("intake");
    setError(null);
    try {
      const card = await stageOperatorUpdate(part.project_id, {
        thread_id: threadId,
        fields,
        reason,
        evidence_ref: evidenceRef || null,
      });
      setOperationId(card.operation_id);
      setStatus("pending");
      onActivity?.(`Staged ${part.project_id} milestone update ${card.operation_id}.`);
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setPending(null);
    }
  }

  async function decide(decision: "confirm" | "cancel") {
    if (!threadId || !operationId) {
      setError("This update has no operation id yet.");
      return;
    }
    setPending(decision);
    setError(null);
    setStatus("executing");
    try {
      if (decision === "confirm") {
        const result = await confirmOperatorUpdate(part.project_id, operationId, {
          thread_id: threadId,
          reason,
          evidence_ref: evidenceRef || null,
        });
        const outcome = String(result.outcome || "accepted");
        setStatus("confirmed");
        onActivity?.(`Confirmed ${part.project_id} milestone update (${outcome}).`);
      } else {
        await cancelOperatorUpdate(part.project_id, operationId, { thread_id: threadId });
        setStatus("cancelled");
        onActivity?.(`Cancelled ${part.project_id} milestone update.`);
      }
      onResolved?.();
    } catch (caught) {
      const code = caught instanceof ApiError ? caught.errorCode : null;
      if (code === "pending_operation_conflict") setStatus("superseded");
      else {
        setStatus((current) => (current === "executing" ? part.status : current));
      }
      setError(errorMessage(caught));
      if (code === "pending_operation_conflict" || code === "pending_operation_not_pending") {
        try {
          const card = await getOperatorUpdate(part.project_id, operationId);
          setStatus(normalizeCardStatus(card.status));
        } catch {
          // Keep the known conflict/error state if recovery is unavailable.
        }
        onResolved?.();
      }
    } finally {
      setPending(null);
    }
  }

  return (
    <Card className={override ? "border-warning/30 bg-warning/[0.045] shadow-none" : "border-primary/20 bg-primary/[0.035] shadow-none"}>
      <CardHeader className="border-b">
        <div className="flex items-start gap-3">
          <span className={`flex size-10 shrink-0 items-center justify-center rounded-xl ${override ? "bg-warning/15 text-warning" : "bg-primary/10 text-primary"}`}>
            {override ? <ShieldAlert className="size-5" /> : <ShieldCheck className="size-5" />}
          </span>
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <CardTitle>Operator milestone update</CardTitle>
              <Badge variant={override ? "destructive" : "secondary"}>
                {override ? "OVERRIDE" : "Evaluator-aligned"}
              </Badge>
              <Badge variant="outline" className="capitalize">{status}</Badge>
            </div>
            <p className="mt-1 text-xs text-muted-foreground">
              <Link href={`/projects/${encodeURIComponent(part.project_id)}`} className="font-medium text-primary hover:underline">
                {part.project_id}
              </Link>
              {part.transition === "revert" ? " · FAIL revert" : null}
            </p>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="rounded-lg border bg-background/80 p-3 text-sm">
          <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Milestone</p>
          <p className="mt-1">
            <span className="text-muted-foreground line-through">{fromMilestone}</span>
            <span className="mx-2" aria-hidden="true">→</span>
            <span className="font-medium">{toMilestone}</span>
          </p>
        </div>
        {fields.next_action ? (
          <div className="rounded-lg border bg-background/80 p-3 text-sm">
            <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Next action</p>
            <p className="mt-1 font-medium">{fields.next_action}</p>
          </div>
        ) : null}
        {override && part.missing_criteria?.length ? (
          <div className="rounded-lg border border-warning/30 bg-warning/5 p-3">
            <p className="text-xs font-semibold uppercase tracking-wide text-warning">
              Assessment did not pass
            </p>
            <ul className="mt-2 space-y-1 text-sm">
              {part.missing_criteria.map((item) => (
                <li key={item.id}>
                  <span className="font-mono text-xs">{item.id}</span>
                  {item.description ? <span className="ml-2 text-muted-foreground">{item.description}</span> : null}
                </li>
              ))}
            </ul>
          </div>
        ) : null}
        <div>
          <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Reason</p>
          {status === "needs_intake" && canManageActions ? (
            <Textarea
              className="mt-1"
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              placeholder="Why is this override valid? (20+ characters)"
            />
          ) : (
            <p className="mt-1 text-sm">{reason || "No reason recorded."}</p>
          )}
        </div>
        {status === "needs_intake" && canManageActions && evidence.length ? (
          <label className="block text-xs">
            <span className="font-semibold uppercase tracking-wide text-muted-foreground">Evidence (optional)</span>
            <select
              className="mt-1 w-full rounded-md border bg-background px-2 py-2 text-sm"
              value={evidenceRef}
              onChange={(event) => setEvidenceRef(event.target.value)}
            >
              <option value="">Operator note will be created</option>
              {evidence.map((item) => (
                <option key={item.evidence_id} value={item.evidence_id}>
                  {item.content_summary || item.evidence_id}
                </option>
              ))}
            </select>
          </label>
        ) : (
          <p className="text-xs text-muted-foreground">
            {evidenceRef ? `Evidence ${evidenceRef}` : resolved ? "See the recorded operation outcome." : "Operator note will be created on confirm."}
          </p>
        )}
        {part.base_version ? (
          <p className="text-xs text-muted-foreground">CAS {part.base_version}</p>
        ) : null}
        {part.expires_at && !resolved ? (
          <p className="text-xs text-muted-foreground">Expires {new Date(part.expires_at).toLocaleString()}</p>
        ) : null}
        {error ? (
          <p role="alert" className="flex items-center gap-2 text-sm text-destructive">
            <AlertTriangle className="size-4" />
            {error}
          </p>
        ) : null}
        {!canManageActions && !resolved ? (
          <p className="text-sm text-muted-foreground">
            Your role can review this proposal but cannot confirm milestone changes.
          </p>
        ) : null}
      </CardContent>
      {canManageActions && status === "needs_intake" ? (
        <CardFooter className="justify-end gap-2">
          <Button type="button" variant="outline" onClick={() => setDismissed(true)}>
            Dismiss
          </Button>
          <Button type="button" onClick={() => void submitIntake()} disabled={pending !== null}>
            {pending === "intake" ? <Loader2 className="animate-spin" /> : <Check />}
            Stage override
          </Button>
        </CardFooter>
      ) : null}
      {canManageActions && status === "pending" ? (
        <CardFooter className="justify-end gap-2">
          <Button type="button" variant="outline" onClick={() => void decide("cancel")} disabled={pending !== null}>
            {pending === "cancel" ? <Loader2 className="animate-spin" /> : <X />}
            Cancel
          </Button>
          <Button type="button" onClick={() => void decide("confirm")} disabled={pending !== null}>
            {pending === "confirm" ? <Loader2 className="animate-spin" /> : <Check />}
            Confirm
          </Button>
        </CardFooter>
      ) : null}
      {status === "executing" ? (
        <CardFooter className="gap-2 text-sm text-muted-foreground">
          <Loader2 className="size-4 animate-spin" />
          Applying update
        </CardFooter>
      ) : null}
      {status === "confirmed" ? (
        <CardFooter className="gap-2 text-sm font-medium text-success">
          <CheckCircle2 className="size-4" />
          Milestone update confirmed
        </CardFooter>
      ) : null}
      {status === "cancelled" ? (
        <CardFooter className="text-sm text-muted-foreground">Update cancelled</CardFooter>
      ) : null}
      {status === "superseded" ? (
        <CardFooter className="text-sm text-muted-foreground">Replaced by a newer proposal</CardFooter>
      ) : null}
      {status === "expired" ? (
        <CardFooter className="text-sm text-muted-foreground">This update expired</CardFooter>
      ) : null}
      {status === "failed" ? (
        <CardFooter className="text-sm text-destructive">Update failed</CardFooter>
      ) : null}
    </Card>
  );
}
