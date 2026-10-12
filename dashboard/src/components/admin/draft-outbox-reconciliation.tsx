"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import { RefreshCw } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  ApiError, getDraftOutboxReconciliation, reconcileDraftOutbox,
  type DraftOutboxReconciliationItem, type DraftOutboxReconcileRequest,
} from "@/lib/api";

function RecipientSet({ recipients }: { recipients: NonNullable<DraftOutboxReconciliationItem["recipients"]> }) {
  return (
    <dl className="grid gap-2 rounded-lg border bg-muted/30 p-3 text-sm sm:grid-cols-[3rem_1fr]">
      {(["to", "cc", "bcc"] as const).map((bucket) => (
        <div key={bucket} className="contents">
          <dt className="font-semibold uppercase">{bucket}</dt>
          <dd className="min-w-0 break-all">{recipients[bucket].length ? recipients[bucket].join(", ") : "(none)"}</dd>
        </div>
      ))}
    </dl>
  );
}

function ReconciliationCard({ item, onRefresh }: { item: DraftOutboxReconciliationItem; onRefresh: () => Promise<void> }) {
  const [decision, setDecision] = useState<DraftOutboxReconcileRequest["decision"] | null>(null);
  const [providerEvidence, setProviderEvidence] = useState("");
  const [recipientEvidence, setRecipientEvidence] = useState("");
  const [providerMessageId, setProviderMessageId] = useState("");
  const [verified, setVerified] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [conflict, setConflict] = useState(false);
  const [recorded, setRecorded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const blocked = Boolean(item.review_blocked_reason || !item.recipients || item.subject === null);
  const valid = !blocked && !conflict && !recorded && !!decision && verified
    && !!providerEvidence.trim() && !!recipientEvidence.trim()
    && providerEvidence.trim() !== recipientEvidence.trim()
    && (decision !== "confirmed_sent" || !!providerMessageId.trim());

  function review(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (valid && !busy) {
      setError(null);
      setConfirmOpen(true);
    }
  }

  async function confirm() {
    if (!valid || !decision || busy) return;
    setBusy(true);
    setError(null);
    const body: DraftOutboxReconcileRequest = {
      decision,
      expected_updated_at: item.updated_at,
      expected_attempt_count: item.attempt_count,
      provider_evidence_ref: providerEvidence.trim(),
      recipient_evidence_ref: recipientEvidence.trim(),
      full_recipient_set_verified: true,
      ...(decision === "confirmed_sent" ? { provider_message_id: providerMessageId.trim() } : {}),
    };
    try {
      await reconcileDraftOutbox(item.outbox_id, body);
      setConfirmOpen(false);
      setRecorded(true);
      await onRefresh();
    } catch (cause) {
      setConfirmOpen(false);
      if (cause instanceof ApiError && cause.status === 409) {
        setConflict(true);
        setError(`Review is stale or conflicts with another delivery: ${cause.message} Refresh before retrying.`);
      } else {
        setError(cause instanceof Error ? cause.message : "Could not record the decision.");
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-start justify-between gap-2">
          <CardTitle className="min-w-0 break-words text-base">{item.subject ?? "Snapshot unavailable"}</CardTitle>
          <Badge variant="outline" className="border-warning/40 bg-warning/10 text-warning">Uncertain</Badge>
        </div>
        <CardDescription className="break-all">Draft {item.draft_id} · Outbox {item.outbox_id}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4 text-sm">
        <div className="grid gap-1 text-muted-foreground sm:grid-cols-2">
          <p>Created by: {item.created_by}</p><p>Attempts: {item.attempt_count}</p>
          <p>Created: {item.created_at}</p><p>Updated: {item.updated_at}</p>
        </div>
        <p className="break-all">Snapshot hash: <code className="font-mono text-xs">{item.snapshot_hash}</code></p>
        <div className="rounded-lg border border-warning/30 bg-warning/10 p-3">
          <p><strong>Status:</strong> {item.status}</p>
          <p className="break-words"><strong>Last error:</strong> {item.last_error ?? "No error details recorded"}</p>
          <p className="break-all"><strong>Provider result:</strong> {item.provider_result ? JSON.stringify(item.provider_result) : "None recorded"}</p>
        </div>
        {item.recipients ? <div className="space-y-2"><h3 className="font-semibold">Original recipient set</h3><RecipientSet recipients={item.recipients} /></div> : null}
        {blocked ? (
          <p role="alert" className="rounded-lg border border-danger/30 bg-danger/10 p-3 text-danger">
            Review blocked: {item.review_blocked_reason ?? "Original snapshot recipients or subject are unavailable."} No decision can be recorded here.
          </p>
        ) : (
          <form className="space-y-4 border-t pt-4" onSubmit={review}>
            <p className="text-muted-foreground">Investigate provider delivery and independently verify every original To, Cc, and Bcc recipient. This screen never sends or retries email.</p>
            <fieldset disabled={busy || conflict || recorded} className="space-y-3">
              <legend className="font-semibold">Manual delivery decision</legend>
              <label className="flex items-center gap-2"><input type="radio" name={`decision-${item.outbox_id}`} checked={decision === "confirmed_sent"} onChange={() => setDecision("confirmed_sent")} />Confirm sent</label>
              <label className="flex items-center gap-2"><input type="radio" name={`decision-${item.outbox_id}`} checked={decision === "confirmed_not_sent"} onChange={() => setDecision("confirmed_not_sent")} />Confirm not sent</label>
              {decision === "confirmed_sent" ? (
                <label className="block space-y-1 font-medium">Provider message ID
                  <Input value={providerMessageId} onChange={(event) => setProviderMessageId(event.target.value)} maxLength={1000} required />
                </label>
              ) : null}
              <label className="block space-y-1 font-medium">Provider evidence reference
                <Input value={providerEvidence} onChange={(event) => setProviderEvidence(event.target.value)} maxLength={2000} required />
              </label>
              <label className="block space-y-1 font-medium">Recipient evidence reference
                <Input value={recipientEvidence} onChange={(event) => setRecipientEvidence(event.target.value)} maxLength={2000} required />
              </label>
              <label className="flex items-start gap-2 font-medium">
                <input type="checkbox" checked={verified} onChange={(event) => setVerified(event.target.checked)} className="mt-1 size-4 accent-primary" />
                I independently verified the full original To, Cc, and Bcc recipient set.
              </label>
            </fieldset>
            {error ? <p role="alert" className="text-danger">{error}</p> : null}
            {recorded ? <p role="status" className="text-success">Decision recorded. Refresh the list to see the latest state.</p> : null}
            {conflict ? <Button type="button" variant="outline" onClick={() => void onRefresh()}>Refresh stale review</Button> : null}
            <Button type="submit" disabled={!valid || busy}>Review decision</Button>
          </form>
        )}
        <Dialog open={confirmOpen} onOpenChange={(open) => { if (!busy) setConfirmOpen(open); }}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>Confirm manual reconciliation</DialogTitle>
              <DialogDescription>This records a final operator decision; it does not send or retry email. Compare the immutable recipients and evidence one last time.</DialogDescription>
            </DialogHeader>
            <div className="mt-4 space-y-3 text-sm">
              <p><strong>Decision:</strong> {decision === "confirmed_sent" ? "Confirmed sent" : "Confirmed not sent"}</p>
              {decision === "confirmed_sent" ? <p className="break-all"><strong>Provider message ID:</strong> {providerMessageId.trim()}</p> : null}
              {item.recipients ? <RecipientSet recipients={item.recipients} /> : null}
              <p className="break-all"><strong>Provider evidence:</strong> {providerEvidence.trim()}</p>
              <p className="break-all"><strong>Recipient evidence:</strong> {recipientEvidence.trim()}</p>
            </div>
            <DialogFooter>
              <Button type="button" variant="outline" disabled={busy} onClick={() => setConfirmOpen(false)}>Cancel</Button>
              <Button type="button" disabled={!valid || busy} onClick={() => void confirm()}>{busy ? "Recording..." : "Confirm and record outcome"}</Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      </CardContent>
    </Card>
  );
}

export function DraftOutboxReconciliation() {
  const [items, setItems] = useState<DraftOutboxReconciliationItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setItems(await getDraftOutboxReconciliation());
      setRevision((current) => current + 1);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not load uncertain draft deliveries.");
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => { void refresh(); }, [refresh]);

  return (
    <section aria-label="Draft outbox reconciliation" className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div><h2 className="text-lg font-semibold">Uncertain draft deliveries</h2>
          <p className="text-sm text-muted-foreground">Evidence-bound operator review only. Never automatically resend an uncertain delivery.</p></div>
        <Button type="button" size="sm" variant="outline" disabled={loading} onClick={() => void refresh()}><RefreshCw className={loading ? "size-4 animate-spin" : "size-4"} />Refresh</Button>
      </div>
      {error ? <p role="alert" className="text-danger">{error}</p> : null}
      {loading ? <p role="status" className="text-muted-foreground">Loading uncertain deliveries...</p> : null}
      {!loading && !error && items.length === 0 ? <p className="rounded-lg border border-dashed p-6 text-muted-foreground">No uncertain draft deliveries need review.</p> : null}
      {!loading && !error && <div className="grid gap-4 xl:grid-cols-2">{items.map((item) => <ReconciliationCard key={`${item.outbox_id}:${revision}`} item={item} onRefresh={refresh} />)}</div>}
    </section>
  );
}
