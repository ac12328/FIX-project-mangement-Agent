"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import { RefreshCw } from "lucide-react";

import { ApiError, getReviewableReminderDeliveries, reconcileReminderDelivery, reviewReminderLinkedItem, type ReminderDeliveryReviewItem } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { useEvents } from "@/lib/useEvents";

const REMINDER_EVENTS = ["personal_reminder_delivery_reviewed", "personal_reminder_linked_item_reviewed"];

type LinkedDecision = "retry_resolution" | "acknowledge_manual_followup";
type TelegramDecision = "confirm_sent" | "confirm_not_sent";

function ReviewCard({ receipt, onReviewed }: {
  receipt: ReminderDeliveryReviewItem;
  onReviewed: () => Promise<void>;
}) {
  const [evidence, setEvidence] = useState("");
  const [note, setNote] = useState("");
  const [messageIds, setMessageIds] = useState("");
  const [linkedDecision, setLinkedDecision] = useState<LinkedDecision>("retry_resolution");
  const [telegramDecision, setTelegramDecision] = useState<TelegramDecision>("confirm_sent");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const isLinkedFailure = receipt.state === "sent";
  const actionable = receipt.chat_id !== null && receipt.bot_fingerprint !== null && receipt.state !== "unrecorded";

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!actionable || busy) return;
    setError(null);
    const identity = {
      expected_version: receipt.review_version,
      expected_chat_id: receipt.chat_id!,
      expected_bot_fingerprint: receipt.bot_fingerprint!,
      evidence_ref: evidence.trim(),
    };
    let ids: number[] | null = null;
    if (!isLinkedFailure && telegramDecision === "confirm_sent") {
      const parts = messageIds.split(/[\s,]+/).filter(Boolean);
      ids = parts.map(Number);
      if (!parts.length || ids.some((id) => !Number.isSafeInteger(id) || id <= 0)
          || new Set(ids).size !== ids.length || ids.length > 100) {
        setError("Enter 1 to 100 distinct positive Telegram message IDs.");
        return;
      }
    }
    setBusy(true);
    try {
      if (isLinkedFailure) {
        await reviewReminderLinkedItem(receipt.run_id, {
          ...identity, decision: linkedDecision, note: note.trim(),
        });
      } else {
        await reconcileReminderDelivery(receipt.run_id, {
          ...identity, decision: telegramDecision, telegram_message_ids: ids,
        });
      }
      await onReviewed();
    } catch (cause) {
      setError(cause instanceof ApiError && cause.status === 409
        ? `Receipt changed or action is no longer safe: ${cause.message} Refresh before retrying.`
        : cause instanceof Error ? cause.message : "Review could not be saved.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle className="text-base">{receipt.title}</CardTitle>
          <Badge variant="outline" className={isLinkedFailure ? "border-warning/40 text-warning" : "border-info/40 text-info"}>
            {isLinkedFailure ? "Sent - local item failed" : receipt.state === "unrecorded" ? "Historical claim" : "Send outcome unknown"}
          </Badge>
        </div>
        <CardDescription>Operator {receipt.operator_id} - Run <span className="font-mono">{receipt.run_id}</span> - Due {receipt.scheduled_for}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4 text-sm">
        <div className="rounded-md border border-warning/30 bg-warning/10 px-3 py-2 text-foreground">
          <span className="font-medium">Why review is needed: </span>{receipt.last_error ?? "Outcome not confirmed"}
        </div>
        {receipt.state === "unrecorded" ? (
          <p className="text-muted-foreground">This historical claim has no durable send intent. Investigate Telegram audit externally; it cannot be reconciled from this screen.</p>
        ) : (
          <form className="space-y-4" onSubmit={(event) => void submit(event)}>
            <fieldset disabled={busy} className="space-y-3">
              <legend className="font-medium">{isLinkedFailure ? "Local linked-item review" : "Telegram delivery attestation"}</legend>
              {isLinkedFailure ? (
                <>
                  <p className="text-muted-foreground">Telegram was already sent. Neither choice sends another message or alters its receipt.</p>
                  <label className="flex items-start gap-2"><input type="radio" name={`decision-${receipt.run_id}`} checked={linkedDecision === "retry_resolution"} onChange={() => setLinkedDecision("retry_resolution")} />
                    <span><strong>Retry local resolution</strong> - resolve the linked item only if it is still open and no later schedule intent exists.</span></label>
                  <label className="flex items-start gap-2"><input type="radio" name={`decision-${receipt.run_id}`} checked={linkedDecision === "acknowledge_manual_followup"} onChange={() => setLinkedDecision("acknowledge_manual_followup")} />
                    <span><strong>Document manual follow-up</strong> - leave the work item unchanged and remove this receipt from the review queue.</span></label>
                  <label className="block space-y-1 font-medium" htmlFor={`note-${receipt.run_id}`}>Review note
                    <Textarea id={`note-${receipt.run_id}`} value={note} onChange={(event) => setNote(event.target.value)} required maxLength={2000} placeholder="Describe your local check or follow-up owner" />
                  </label>
                </>
              ) : (
                <>
                  <p className="text-muted-foreground">Check independent Telegram evidence first. This does not retry delivery.</p>
                  <label className="flex items-start gap-2"><input type="radio" name={`decision-${receipt.run_id}`} checked={telegramDecision === "confirm_sent"} onChange={() => setTelegramDecision("confirm_sent")} />Confirm sent</label>
                  <label className="flex items-start gap-2"><input type="radio" name={`decision-${receipt.run_id}`} checked={telegramDecision === "confirm_not_sent"} onChange={() => setTelegramDecision("confirm_not_sent")} />Confirm not sent</label>
                  {telegramDecision === "confirm_sent" ? (
                    <label className="block space-y-1 font-medium" htmlFor={`ids-${receipt.run_id}`}>Telegram message IDs (comma-separated)
                      <Input id={`ids-${receipt.run_id}`} value={messageIds} onChange={(event) => setMessageIds(event.target.value)} required placeholder="12345, 12346" />
                    </label>
                  ) : null}
                </>
              )}
              <label className="block space-y-1 font-medium" htmlFor={`evidence-${receipt.run_id}`}>Independent evidence reference
                <Input id={`evidence-${receipt.run_id}`} value={evidence} onChange={(event) => setEvidence(event.target.value)} required maxLength={2000} placeholder="Incident or audit reference" />
              </label>
            </fieldset>
            {error ? <p role="alert" className="text-danger">{error}</p> : null}
            <Button type="submit" disabled={busy || !evidence.trim() || (isLinkedFailure && !note.trim())}>
              {busy ? "Saving review..." : isLinkedFailure ? "Record linked-item review" : "Record Telegram outcome"}
            </Button>
          </form>
        )}
      </CardContent>
    </Card>
  );
}

export function ReminderDeliveries() {
  const [receipts, setReceipts] = useState<ReminderDeliveryReviewItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setReceipts(await getReviewableReminderDeliveries());
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not load reminder receipts.");
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => { void refresh(); }, [refresh]);
  useEvents({
    eventTypes: REMINDER_EVENTS,
    onRefresh: () => { void refresh(); },
    onReconnect: () => { void refresh(); },
  });

  return (
    <section aria-label="Reminder delivery reviews" className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div><h2 className="text-lg font-semibold">Reminder delivery reviews</h2>
          <p className="text-sm text-muted-foreground">Investigate uncertain sends separately from confirmed sends with local linked-item failures.</p></div>
        <Button type="button" size="sm" variant="outline" onClick={() => void refresh()} disabled={loading}>
          <RefreshCw className={loading ? "size-4 animate-spin" : "size-4"} />Refresh
        </Button>
      </div>
      {error ? <p role="alert" className="text-danger">{error}</p> : null}
      {loading ? <p role="status" className="text-muted-foreground">Loading reminder reviews...</p> : null}
      {!loading && !error && receipts.length === 0 ? <p className="rounded-lg border border-dashed p-6 text-sm text-muted-foreground">No reminder receipts need review.</p> : null}
      <div className="grid gap-4 xl:grid-cols-2">
        {receipts.map((receipt) => <ReviewCard key={receipt.run_id} receipt={receipt} onReviewed={refresh} />)}
      </div>
    </section>
  );
}
