import { editRows, editable, type ActionChange, type ActionEdit, type ActionPlan, type PlanBundle, type PlanBundleResult } from "./action-plan";
export interface ActionReview {
  review_id: string; project_id: string; source_ref: string; review_reason: string;
  status: "pending" | "approved" | "rejected"; revision: number; created_at: string;
  source_current: boolean; source_authorized: boolean;
  source: { sender: string; body: string; actor: string; evidence_level: string; authored_at: string | null };
  changes: { change: ActionChange; citations: { quote: string; modality?: string }[]; extraction_confidence: number }[];
}
export interface ReviewDecision {
  decision_id: string; expected_revision: number; decision: "approved" | "rejected"; reason: string;
  bundle?: Omit<PlanBundle, "review_context">;
}
export interface ReviewDecisionResult {
  outcome: "applied" | "duplicate"; review_id: string; status: "approved" | "rejected";
  decision_id: string; action_result: PlanBundleResult | null;
}
export function stageActionReview(plan: ActionPlan, review: ActionReview): ActionEdit[] {
  if (review.project_id !== plan.project_id || review.status !== "pending") throw new Error("Review is not pending for this project.");
  if (!review.source_current || !review.source_authorized) throw new Error("Reassess source identity or changed email before approval.");
  const rows = editRows(plan); const seen = new Set<string>();
  for (const { change } of review.changes) {
    if (seen.has(change.action_id)) throw new Error("Duplicate proposed action identity requires clarification.");
    seen.add(change.action_id);
    if (change.operation === "create") {
      if (!change.action_id.startsWith("new:") || rows.some(row => row.followup_id === change.action_id)) throw new Error("Invalid new action reference.");
      rows.push({ followup_id: change.action_id, description: change.description || "", owner_side: change.owner_side ?? null,
        due_date: change.due_date ?? null, reminder_due_at: change.reminder_due_at ?? null, depends_on: change.depends_on || [],
        status: "open", action_revision: 0, decision: "keep" });
      continue;
    }
    const row = rows.find(item => item.followup_id === change.action_id);
    if (!row || !editable(row) || row.action_revision !== change.expected_revision) throw new Error("An action changed or closed since extraction. Reassess rather than importing stale instructions.");
    if (change.operation === "complete" || change.operation === "cancel") row.decision = change.operation;
    else {
      if (change.description !== undefined) row.description = change.description ?? "";
      if (change.owner_side !== undefined) row.owner_side = change.owner_side;
      if (change.due_date !== undefined) row.due_date = change.due_date;
      if (change.reminder_due_at !== undefined) row.reminder_due_at = change.reminder_due_at;
      if (change.depends_on != null) row.depends_on = [...change.depends_on];
    }
  }
  return rows;
}
