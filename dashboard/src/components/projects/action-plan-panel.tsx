"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useAuth } from "@/lib/auth-context";
import { useEvents } from "@/lib/useEvents";
import { ApiError, getActionPlan, applyActionPlanBundle } from "@/lib/api";
import { ActionReviewInbox } from "./action-review-inbox";
import { stageActionReview, type ActionReview } from "@/lib/action-review";
import { recoveryKey, readPendingBundle, savePendingBundle, clearPendingBundle } from "@/lib/action-plan-recovery";
import { buildPlanChanges, editable, editRows, type ActionPlan, type ActionEdit, type PlanBundle, type ActionOwner, type PlanAction } from "@/lib/action-plan";

export function ActionPlanPanel({ projectId }: { projectId: string }) {
  const { session } = useAuth();
  const journalKey = recoveryKey(session, projectId);
  return <ProjectActionPlan key={`${projectId}:${journalKey}`} projectId={projectId} journalKey={journalKey} />;
}

function ProjectActionPlan({ projectId, journalKey }: { projectId: string; journalKey: string | null }) {
  const { hasPermission } = useAuth();
  const canWrite = hasPermission("business.write");
  const [plan, setPlan] = useState<ActionPlan | null>(null);
  const [rows, setRows] = useState<ActionEdit[] | null>(null);
  const [reason, setReason] = useState("");
  const [evidence, setEvidence] = useState("");
  const [replace, setReplace] = useState(false);
  const [review, setReview] = useState<PlanBundle | null>(null);
  const [reviewContext, setReviewContext] = useState<PlanBundle["review_context"]>();
  const [uncertain, setUncertain] = useState(false);
  const [stale, setStale] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const generation = useRef(0);
  const mounted = useRef(true);
  const editing = useRef(false);
  editing.current = rows !== null;
  const load = useCallback(async () => {
    const version = ++generation.current;
    try {
      const data = await getActionPlan(projectId);
      if (mounted.current && version === generation.current && !editing.current) {
        const pending = journalKey ? readPendingBundle(journalKey, projectId) : null;
        setPlan(data); setError("");
        if (pending) {
          editing.current = true; setRows(editRows(data)); setReview(pending); setUncertain(true);
          setReviewContext(pending.review_context);
          setReason(pending.reason); setEvidence(pending.evidence_ref || ""); setReplace(pending.replace_legacy_summary);
          setNotice("Recovered an earlier submission. The current plan may already include it. Nothing is sent automatically; retry the exact bundle to confirm its receipt.");
        }
      }
    } catch (err) {
      if (mounted.current && version === generation.current) setError(err instanceof Error ? err.message : "Could not load the action plan.");
    }
  }, [projectId, journalKey]);
  const invalidateLoads = useCallback(() => { ++generation.current; }, []);
  useEffect(() => {
    mounted.current = true; void load();
    return () => { mounted.current = false; invalidateLoads(); };
  }, [load, invalidateLoads]);
  useEvents({ eventTypes: ["project_updated"], onEvent: event => {
    if (event.project_id !== projectId) return;
    if (["pending", "rejected"].includes(String(event.detail.action_review_status))) return;
    if (editing.current) setStale(true); else void load();
  }, onReconnect: () => { if (editing.current) setStale(true); else void load(); } });
  const terminal = !!plan && ["Completed", "Cancelled", "Archived"].includes(plan.project_status);
  const locked = busy || !!review || !canWrite || terminal;
  const needsAdoption = plan?.revision === 0 && !!plan.next_action;
  function change(id: string, fields: Partial<ActionEdit>) {
    setRows(current => current?.map(row => row.followup_id === id ? { ...row, ...fields } : row) ?? null); setError("");
  }
  function begin() {
    if (!plan) return;
    ++generation.current; setReviewContext(undefined); setRows(editRows(plan)); setReason(""); setEvidence(""); setReplace(false);
    setStale(false); setError(""); setNotice("");
  }
  function adopt(item: ActionReview) {
    if (!plan || rows || busy || !canWrite || !journalKey) return;
    try {
      const staged = stageActionReview(plan, item);
      ++generation.current; setRows(staged); setReason(""); setEvidence(""); setReplace(false);
      setReviewContext({ review_id: item.review_id, expected_revision: item.revision, decision_id: "action-review-" + crypto.randomUUID() });
      setError(""); setStale(false); setNotice("Email proposal staged, not approved. Verify every action and record your reasoning before previewing.");
    } catch (err) { setError(err instanceof Error ? err.message : "Proposal cannot be staged."); }
  }
  function discard() { setReviewContext(undefined); setRows(null); editing.current = false; setReview(null); setStale(false); setError(""); void load(); }
  function preview() {
    if (!plan || !rows) return;
    try {
      const actions = buildPlanChanges(plan, rows);
      if (!actions.length) throw new Error("No action changes to submit.");
      if (!reason.trim()) throw new Error("Record why these action changes are correct.");
      if (needsAdoption && !replace) throw new Error("Acknowledge replacement of the existing next-action summary.");
      setReview({ operation_id: "dashboard-plan-" + crypto.randomUUID(), expected_plan_revision: plan.revision,
        expected_project_version: plan.project_version, reason: reason.trim(), evidence_ref: evidence.trim() || undefined,
        replace_legacy_summary: replace, actions, review_context: reviewContext }); setError("");
    } catch (err) { setError(err instanceof Error ? err.message : "Invalid action plan."); }
  }
  async function submit() {
    if (!review || busy || !canWrite) return;
    if (!journalKey) { setError("No authenticated session boundary is available for submission recovery. No request was sent."); return; }
    setBusy(true); setError("");
    try { savePendingBundle(journalKey, projectId, review); }
    catch (err) { setError(`No request was sent: ${err instanceof Error ? err.message : "Recovery storage is unavailable."}`); setBusy(false); return; }
    try {
      const result = await applyActionPlanBundle(projectId, review);
      if (result.current_plan?.project_id !== projectId || result.operation_id !== review.operation_id
          || !["applied", "duplicate"].includes(result.outcome)) throw new Error("Mismatched action bundle receipt");
      const cleared = clearPendingBundle(journalKey, projectId, review.operation_id, review.review_context?.decision_id);
      if (!mounted.current) return;
      ++generation.current; setPlan(result.current_plan); setRows(cleared ? null : editRows(result.current_plan)); editing.current = !cleared;
      setReview(cleared ? null : review); setReviewContext(cleared ? undefined : review.review_context); setUncertain(!cleared); setStale(false);
      if (!cleared) setError("The server receipt is confirmed, but browser recovery cleanup failed. Retry the exact bundle to clear it before editing again.");
      setNotice(review.review_context ? "Email action review approved; the confirmed current plan is shown." : result.outcome === "duplicate" ? "This bundle was already committed; the current plan is shown." : "Action bundle committed. Milestone and project status were not changed.");
    } catch (err) {
      const rejected = err instanceof ApiError && err.status !== null && [400, 401, 403, 404, 409, 422].includes(err.status);
      const cleared = rejected ? clearPendingBundle(journalKey, projectId, review.operation_id, review.review_context?.decision_id) : false;
      if (!mounted.current) return;
      if (rejected) {
        setReview(cleared ? null : review); setUncertain(!cleared); setStale(err.status === 409);
        setError(`${err.message} Your edits have been retained.${cleared ? "" : " Local recovery cleanup failed; retry this bundle before editing again."}`);
      } else {
        setUncertain(true); setError("Delivery outcome is unknown. Do not create a replacement bundle. Retry this exact bundle to check its committed receipt.");
      }
    } finally { if (mounted.current) setBusy(false); }
  }
  if (!plan) return <Card><CardContent className="p-4"><p role={error ? "alert" : "status"}>{error || "Loading action plan..."}</p>{error && <Button variant="outline" onClick={() => void load()}>Retry loading</Button>}</CardContent></Card>;
  const displayed = rows || editRows(plan);
  function valueText(value: unknown): string {
    if (Array.isArray(value)) return value.map(id => displayed.find(row => row.followup_id === id)?.description || id).join("; ") || "None";
    return String(value ?? "None");
  }
  function beforeText(actionId: string, field: string): string {
    const old = plan?.actions.find(action => action.followup_id === actionId);
    return old && !uncertain ? valueText(old[field as keyof PlanAction]) + " -> " : "";
  }
  return <Card><CardHeader><CardTitle>Action plan</CardTitle><p className="text-sm text-muted-foreground">Revision {plan.revision} / Next-step owner: {plan.ball_with}</p></CardHeader>
    <CardContent className="space-y-4">
      <p className="text-sm">{plan.next_action || "No next action recorded."}</p>
      <p className="text-sm text-muted-foreground">Commitment dates require an explicit promise. Reminders are separate. Only completed prerequisites release dependent work. Closing tasks does not complete onboarding.</p>
      {terminal && <p role="status" className="rounded-md border p-3 text-sm">This onboarding project is closed. Track remaining work in linked support tickets.</p>}
      {notice && <p role="status" className="rounded-md border p-3 text-sm">{notice}</p>}
      {error && <p role="alert" className="rounded-md border border-destructive p-3 text-sm text-destructive">{error}</p>}
      {stale && <p role="status" className="rounded-md border p-3 text-sm">The project may have changed since editing began. Edits are preserved; reload the latest plan before submitting a new bundle.</p>}
      <ActionReviewInbox projectId={projectId} canWrite={canWrite} disabled={!!rows || busy || !journalKey} terminal={terminal}
        actionNames={Object.fromEntries(plan.actions.map(action => [action.followup_id, action.description]))}
        refreshToken={`${plan.revision}:${notice}`} onAdopt={adopt} />
      {!rows && canWrite && !terminal && !journalKey && <p role="alert">An authenticated session is required for safe submission recovery.</p>}
      {!rows && canWrite && !terminal && <Button disabled={!journalKey} onClick={begin}>Edit action plan</Button>}
      {!displayed.length && <p className="text-sm text-muted-foreground">No recorded actions.</p>}
      <div className="space-y-3">{displayed.map((row, index) => <ActionRow key={row.followup_id} row={row} index={index} all={displayed}
        editing={!!rows} locked={locked} onChange={fields => change(row.followup_id, fields)}
        onRemove={() => setRows(current => current?.filter(item => item.followup_id !== row.followup_id) ?? null)} />)}</div>
      {rows && <>
        <Button variant="outline" disabled={locked} onClick={() => setRows(current => [...(current || []), { followup_id: "new:" + crypto.randomUUID(), description: "", owner_side: null, due_date: null, reminder_due_at: null, status: "open", action_revision: 0, depends_on: [], decision: "keep" }])}>Add action</Button>
        <label className="block text-sm" htmlFor="plan-reason">Reason for changes</label><Textarea id="plan-reason" maxLength={2000} value={reason} disabled={locked} onChange={event => setReason(event.target.value)} />
        <label className="block text-sm" htmlFor="plan-evidence">Existing project evidence ID (optional)</label><Input id="plan-evidence" maxLength={500} value={evidence} disabled={locked} onChange={event => setEvidence(event.target.value)} />
        {needsAdoption && <label className="flex items-start gap-2 text-sm"><input type="checkbox" checked={replace} disabled={locked} onChange={event => setReplace(event.target.checked)} /><span>Replace the current next-action summary with the projection of these action records.</span></label>}
        {!review && <div className="flex flex-wrap gap-2"><Button disabled={locked || stale || !reason.trim() || (!!needsAdoption && !replace)} onClick={preview}>Preview changes</Button><Button variant="outline" disabled={busy || uncertain} onClick={discard}>{stale ? "Reload latest and discard edits" : "Discard edits"}</Button></div>}
      </>}
      {review && <section aria-label="Review action bundle" className="space-y-3 rounded-md border bg-muted/40 p-4">
        <h3 className="font-medium">Review {review.actions.length} action changes</h3>
        {review.review_context && <p className="break-all text-sm">Approval will resolve email review {review.review_context.review_id} and commit this edited bundle together.</p>}
        {uncertain && <p className="text-sm">Frozen submission based on revision {review.expected_plan_revision}. Current action rows are shown above; do not treat them as proof that this bundle committed.</p>}
        <p className="text-sm">All changes commit together. The server derives the next-step owner from actionable work, not the milestone.</p>
        <ul className="space-y-2 text-sm">{review.actions.map(action => <li key={action.action_id}><strong>{action.operation}: {action.description || displayed.find(row => row.followup_id === action.action_id)?.description || action.action_id}</strong><ul className="ml-4 list-disc">{Object.entries(action).filter(([key]) => !["action_id", "operation", "expected_revision"].includes(key)).map(([key, value]) => <li key={key}>{key.replaceAll("_", " ")}: {beforeText(action.action_id, key)}{valueText(value)}</li>)}</ul></li>)}</ul>
        <p className="break-all text-xs text-muted-foreground">Bundle: {review.operation_id}</p>
        <div className="flex flex-wrap gap-2"><Button disabled={busy || !canWrite || (stale && !uncertain)} onClick={() => void submit()}>{busy ? "Submitting..." : uncertain ? "Retry exact bundle" : "Confirm action bundle"}</Button>{!uncertain && <Button variant="outline" disabled={busy} onClick={() => setReview(null)}>Back to editing</Button>}</div>
      </section>}
    </CardContent></Card>;
}

function ActionRow({ row, index, all, editing, locked, onChange, onRemove }: {
  row: ActionEdit; index: number; all: ActionEdit[]; editing: boolean; locked: boolean;
  onChange: (fields: Partial<ActionEdit>) => void; onRemove: () => void;
}) {
  const prefix = `plan-action-${index}`;
  const isNew = row.followup_id.startsWith("new:");
  return <section aria-label={`Action ${index + 1}`} className="space-y-2 rounded-md border p-3">
    <div className="flex flex-wrap items-center justify-between gap-2"><span className="text-xs text-muted-foreground">Action {index + 1} / {isNew ? "New" : row.status}</span>{isNew && editing && <Button variant="ghost" size="sm" disabled={locked} onClick={onRemove}>Remove unsaved action</Button>}</div>
    {editing && editable(row) ? <>
      {!isNew && <><label className="block text-sm" htmlFor={`${prefix}-decision`}>Disposition</label><select id={`${prefix}-decision`} className="w-full rounded-md border bg-background p-2 text-sm" value={row.decision} disabled={locked} onChange={event => onChange({ decision: event.target.value as ActionEdit["decision"] })}><option value="keep">Keep pending / edit</option><option value="complete">Mark completed</option><option value="cancel">Cancel (not completed)</option></select></>}
      <fieldset disabled={locked || row.decision !== "keep"} className="space-y-2 disabled:opacity-60">
        <label className="block text-sm" htmlFor={`${prefix}-description`}>Description</label><Textarea id={`${prefix}-description`} maxLength={2000} value={row.description} onChange={event => onChange({ description: event.target.value })} />
        <label className="block text-sm" htmlFor={`${prefix}-owner`}>Responsible side</label><select id={`${prefix}-owner`} className="w-full rounded-md border bg-background p-2 text-sm" value={row.owner_side || ""} onChange={event => onChange({ owner_side: (event.target.value || null) as ActionOwner })}><option value="">Unassigned</option><option>Internal</option><option>External</option><option>Shared</option></select>
        <div className="grid gap-2 sm:grid-cols-2"><div><label className="block text-sm" htmlFor={`${prefix}-due`}>Committed date</label><Input id={`${prefix}-due`} type="date" value={row.due_date || ""} onChange={event => onChange({ due_date: event.target.value || null })} /></div><div><label className="block text-sm" htmlFor={`${prefix}-reminder`}>Reminder date</label><Input id={`${prefix}-reminder`} type="date" value={row.reminder_due_at || ""} onChange={event => onChange({ reminder_due_at: event.target.value || null })} /></div></div>
        <fieldset className="rounded border p-2"><legend className="px-1 text-sm">Prerequisites</legend>{all.filter(item => item.followup_id !== row.followup_id).map(dep => <label key={dep.followup_id} className="flex items-start gap-2 py-1 text-sm"><input type="checkbox" checked={row.depends_on.includes(dep.followup_id)} onChange={event => onChange({ depends_on: event.target.checked ? [...row.depends_on, dep.followup_id] : row.depends_on.filter(id => id !== dep.followup_id) })} /><span>{dep.description || "Untitled action"} ({dep.status})</span></label>)}{all.length < 2 && <p className="text-xs text-muted-foreground">Add another action to define a prerequisite.</p>}</fieldset>
      </fieldset>
    </> : <><p className="text-sm font-medium">{row.description}</p><p className="text-xs text-muted-foreground">{row.owner_side || "Unassigned"} / Commitment: {row.due_date || "Not promised"} / Reminder: {row.reminder_due_at || "None"}</p><p className="text-xs">Prerequisites: {row.depends_on.map(id => all.find(item => item.followup_id === id)?.description || id).join("; ") || "None"}</p></>}
  </section>;
}
