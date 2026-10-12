"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { getActionPlanReviews, decideActionPlanReview, ApiError } from "@/lib/api";
import { useEvents } from "@/lib/useEvents";
import type { ActionReview, ReviewDecision } from "@/lib/action-review";

export function ActionReviewInbox({ projectId, canWrite, disabled, terminal = false, actionNames = {}, refreshToken, onAdopt }: {
  projectId: string; canWrite: boolean; disabled: boolean; terminal?: boolean; actionNames?: Record<string, string>; refreshToken: string;
  onAdopt: (review: ActionReview) => void;
}) {
  const [items, setItems] = useState<ActionReview[]>([]);
  const [error, setError] = useState("");
  const [loadError, setLoadError] = useState("");
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<ActionReview | null>(null);
  const [reason, setReason] = useState("");
  const [pending, setPending] = useState<ReviewDecision | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const mounted = useRef(true);
  const sequence = useRef(0);
  const invalidate = useCallback(() => { ++sequence.current; }, []);
  const load = useCallback(async () => {
    const version = ++sequence.current;
    try {
      const result = await getActionPlanReviews(projectId);
      if (mounted.current && version === sequence.current) { setItems(result); setLoadError(""); }
    } catch (err) {
      if (mounted.current && version === sequence.current) setLoadError(err instanceof Error ? err.message : "Could not load action reviews.");
    } finally { if (mounted.current && version === sequence.current) setLoading(false); }
  }, [projectId]);
  useEffect(() => { mounted.current = true; void load(); return () => { mounted.current = false; invalidate(); }; }, [load, refreshToken, invalidate]);
  useEvents({ eventTypes: ["project_updated"], onEvent: event => { if (event.project_id === projectId) void load(); } });
  async function reject() {
    if (!selected || busy || disabled || !canWrite) return;
    const body: ReviewDecision = pending || { decision_id: "action-review-" + crypto.randomUUID(), expected_revision: selected.revision,
      decision: "rejected", reason: reason.trim() };
    if (!body.reason) return;
    setPending(body); setBusy(true); setError("");
    try {
      const result = await decideActionPlanReview(projectId, selected.review_id, body);
      if (result.review_id !== selected.review_id || result.decision_id !== body.decision_id || result.status !== "rejected" || !["applied", "duplicate"].includes(result.outcome)) throw new Error("Mismatched review receipt");
      setSelected(null); setPending(null); setNotice("Proposal rejected. Project actions were not changed."); void load();
    } catch (err) {
      if (err instanceof ApiError && err.status !== null && [400, 401, 403, 404, 409, 422].includes(err.status)) {
        setPending(null); setSelected(null); setError(err.message); void load();
      } else setError("Rejection outcome is unknown. Retry the identical decision; do not assume it was recorded.");
    } finally { setBusy(false); }
  }
  return <section aria-label="Email action reviews" className="space-y-3 rounded-md border p-3">
    <div className="flex flex-wrap items-center justify-between gap-2"><h3 className="font-medium">Email action reviews ({items.length})</h3><Button variant="ghost" size="sm" disabled={busy} onClick={() => void load()}>Refresh reviews</Button></div>
    <p className="text-sm text-muted-foreground">These suggestions have not been applied. Verify the source, then edit and preview the proposed actions before approval.</p>
    {loading && <p role="status" className="text-sm">Loading reviews...</p>}
    {loadError && <p role="alert" className="text-sm text-destructive">{loadError}</p>}
    {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
    {notice && <p role="status" className="text-sm">{notice}</p>}
    {!loading && !items.length && <p className="text-sm text-muted-foreground">No pending email action reviews.</p>}
    {items.map(item => <article key={item.review_id} className="space-y-2 rounded-md border p-3">
      <p className="text-sm font-medium">{item.review_reason}</p>
      <p className="break-all text-xs text-muted-foreground">{item.review_id} / Source: {item.source_ref}</p>
      <p className="text-xs">{item.source.sender} / {item.source.actor} / Authored: {item.source.authored_at || "Unknown"}</p>
      {(!item.source_current || !item.source_authorized) && <p className="text-sm text-destructive">Source changed or identity is unverified. Reassess before approval.</p>}
      <ul className="space-y-2 text-sm">{item.changes.map((proposal, index) => <li key={index}>
        <strong>{proposal.change.operation}: {proposal.change.description || actionNames[proposal.change.action_id] || proposal.change.action_id} ({proposal.change.action_id})</strong>
        {proposal.citations.map((citation, n) => <blockquote key={n} className="mt-1 whitespace-pre-wrap break-words border-l-2 pl-2 text-muted-foreground">{citation.quote}<span className="block text-xs">Extracted modality: {citation.modality || "unknown"}</span></blockquote>)}
      </li>)}</ul>
      <details><summary className="cursor-pointer text-sm">Captured email source</summary><p className="mt-2 whitespace-pre-wrap break-words text-sm">{item.source.body}</p></details>
      {canWrite && <div className="flex flex-wrap gap-2"><Button variant="outline" disabled={disabled || terminal || busy || !!pending || !item.source_current || !item.source_authorized} onClick={() => onAdopt(item)}>Edit proposal before approval</Button><Button variant="ghost" disabled={disabled || busy || !!pending} onClick={() => { setSelected(item); setReason(""); setError(""); }}>Reject proposal</Button></div>}
    </article>)}
    {selected && <section aria-label="Reject action proposal" className="space-y-2 rounded-md border p-3">
      <p className="break-all text-sm">Reject {selected.review_id}</p>
      <label htmlFor="action-review-reason" className="block text-sm">Rejection reason</label><Textarea id="action-review-reason" value={reason} maxLength={2000} disabled={busy || !!pending} onChange={event => setReason(event.target.value)} />
      <div className="flex flex-wrap gap-2"><Button disabled={busy || disabled || !canWrite || (!pending && !reason.trim())} onClick={() => void reject()}>{busy ? "Recording..." : pending ? "Retry identical rejection" : "Confirm rejection"}</Button>{!pending && <Button variant="outline" onClick={() => setSelected(null)}>Cancel rejection</Button>}</div>
    </section>}
  </section>;
}
