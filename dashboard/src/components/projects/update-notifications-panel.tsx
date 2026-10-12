"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ErrorPanel, LoadingPanel } from "@/components/state-panel";
import { useAuth } from "@/lib/auth-context";
import { useEvents } from "@/lib/useEvents";
import { notifyError, notifySuccess } from "@/lib/notifications";
import { getUpdateNotifications, reconcileUpdateNotification, type UpdateNotification, type NotificationReview } from "@/lib/api";

export function UpdateNotificationsPanel({ projectId }: { projectId: string }) {
  const { hasPermission } = useAuth();
  const [items, setItems] = useState<UpdateNotification[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<UpdateNotification | null>(null);
  const [decision, setDecision] = useState<NotificationReview["decision"] | "">("");
  const [reason, setReason] = useState("");
  const [messageId, setMessageId] = useState("");
  const [busy, setBusy] = useState(false);
  const sequence = useRef(0);
  const load = useCallback(async () => {
    const request = ++sequence.current;
    try {
      const result = await getUpdateNotifications(projectId);
      if (request === sequence.current) { setItems(result); setError(""); }
    } catch (err) {
      if (request === sequence.current) setError(err instanceof Error ? err.message : "Could not load notification delivery.");
    } finally { if (request === sequence.current) setLoading(false); }
  }, [projectId]);
  useEffect(() => {
    void load();
    return () => { sequence.current += 1; };
  }, [load]);
  useEvents({ eventTypes: ["email_notification_updated", "project_updated"],
    onEvent: (event) => { if (event.project_id === projectId) void load(); },
    onReconnect: () => { void load(); },
  });
  const canWrite = hasPermission("business.write");
  const validMessageId = Number.isSafeInteger(Number(messageId)) && Number(messageId) > 0;
  const canSubmit = canWrite && selected && decision && reason.trim() && !busy
    && (decision !== "confirm_sent" || validMessageId);
  async function submit() {
    if (!canSubmit || !selected || !decision) return;
    setBusy(true);
    try {
      await reconcileUpdateNotification(projectId, selected.update_id, {
        decision, expected_updated_at: selected.updated_at, reason: reason.trim(),
        ...(decision === "confirm_sent" ? { telegram_message_id: Number(messageId) } : {}),
      });
      notifySuccess("Notification decision recorded. Project changes were not reapplied.");
    } catch (err) { notifyError(err, "Notification review failed. Refresh and review the current state."); }
    finally { setBusy(false); setSelected(null); await load(); }
  }
  if (loading) return <LoadingPanel label="Loading notification delivery" />;
  if (error) return <ErrorPanel description={error} onRetry={() => void load()} />;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Update notification delivery</CardTitle>
        <p className="text-sm text-muted-foreground">These controls affect notification delivery only. They do not approve or reapply project changes. Showing the latest 50 records.</p>
      </CardHeader>
      <CardContent className="space-y-4">
        <Button variant="outline" onClick={() => void load()} disabled={busy}>Refresh delivery status</Button>
        {!items.length && <p className="text-sm text-muted-foreground">No update notifications recorded.</p>}
        <ul className="space-y-3">
          {items.map((item) => (
            <li key={item.update_id} className="rounded-md border p-3 text-sm">
              <p className="break-all font-medium">Update {item.update_id}</p>
              <p>{item.notification_kind === "pct_completed" ? "PCT completion notice" : item.notification_kind === "review_card" ? "Project review card" : "Update notice"}</p>
              <p>Delivery: {item.state} ? Attempts: {item.attempts}</p>
              {item.last_error && <p className="text-muted-foreground">Detail: {item.last_error.replaceAll("_", " ")}</p>}
              {item.state === "uncertain" && <p className="mt-1">Delivery may have succeeded. Check Telegram before confirming non-delivery.</p>}
              {canWrite && ["pending", "uncertain"].includes(item.state) && (
                <Button className="mt-2" variant="outline" disabled={busy} onClick={() => {
                  setSelected(item); setDecision(""); setReason(""); setMessageId("");
                }}>Review notification {item.update_id}</Button>
              )}
            </li>
          ))}
        </ul>
        {canWrite && selected && (
          <section aria-label="Notification review" className="space-y-3 rounded-md border p-4">
            <p className="break-all text-sm font-medium">Review update {selected.update_id}</p>
            <label className="block text-sm" htmlFor="notification-decision">Delivery decision</label>
            <select id="notification-decision" value={decision} disabled={busy}
              className="w-full rounded-md border bg-background p-2 text-sm"
              onChange={(event) => setDecision(event.target.value as NotificationReview["decision"] | "")}>
              <option value="">Choose a verified outcome</option>
              <option value="confirm_sent">I verified it was sent</option>
              <option value="confirm_not_sent">I verified it was not sent ? queue retry</option>
              <option value="cancel">Cancel this notification</option>
              {hasPermission("admin.security") && selected.state === "pending" && <option value="bind_current_destination">Bind current configured destination (Admin)</option>}
            </select>
            {decision === "confirm_sent" && <>
              <label className="block text-sm" htmlFor="notification-message-id">Telegram message ID</label>
              <Input id="notification-message-id" inputMode="numeric" value={messageId} disabled={busy} onChange={(e) => setMessageId(e.target.value)} />
            </>}
            <label className="block text-sm" htmlFor="notification-reason">Verification reason</label>
            <Textarea id="notification-reason" value={reason} maxLength={2000} disabled={busy} onChange={(e) => setReason(e.target.value)} />
            <div className="flex flex-wrap gap-2">
              <Button disabled={!canSubmit} onClick={() => void submit()}>{busy ? "Recording?" : "Record notification decision"}</Button>
              <Button variant="outline" disabled={busy} onClick={() => setSelected(null)}>Close review</Button>
            </div>
          </section>
        )}
      </CardContent>
    </Card>
  );
}
