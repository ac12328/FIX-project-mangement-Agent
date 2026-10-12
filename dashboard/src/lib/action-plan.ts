export type ActionOwner = "Internal" | "External" | "Shared" | null;
export interface PlanAction {
  followup_id: string; description: string; owner_side: ActionOwner;
  due_date: string | null; reminder_due_at: string | null;
  status: "open" | "overdue" | "completed" | "cancelled";
  action_revision: number; depends_on: string[];
}
export interface ActionPlan {
  project_id: string; project_status: string; revision: number; project_version: string;
  ball_with: string; next_action: string | null; actions: PlanAction[];
}
export interface ActionChange {
  action_id: string; operation: "create" | "update" | "complete" | "cancel";
  expected_revision?: number; description?: string | null; owner_side?: ActionOwner;
  due_date?: string | null; reminder_due_at?: string | null; depends_on?: string[] | null;
}
export interface PlanBundle {
  operation_id: string; expected_plan_revision: number; expected_project_version: string;
  reason: string; evidence_ref?: string; replace_legacy_summary: boolean; actions: ActionChange[];
  review_context?: { review_id: string; expected_revision: number; decision_id: string };
}
export interface PlanBundleResult { outcome: "applied" | "duplicate"; current_plan: ActionPlan; operation_id: string }
export interface ActionEdit extends PlanAction { decision: "keep" | "complete" | "cancel" }
export const editable = (action: PlanAction) => ["open", "overdue"].includes(action.status);
export const editRows = (plan: ActionPlan): ActionEdit[] => plan.actions.map(action => ({ ...action, depends_on: [...action.depends_on], decision: "keep" }));

export function buildPlanChanges(plan: ActionPlan, rows: ActionEdit[]): ActionChange[] {
  const graphRows = rows.map(row => {
    const old = plan.actions.find(action => action.followup_id === row.followup_id);
    return old && (row.decision !== "keep" || !editable(old)) ? old : row;
  });
  const byId = new Map(graphRows.map(row => [row.followup_id, row]));
  if (byId.size !== rows.length) throw new Error("Duplicate action identity.");
  const visited = new Set<string>(); const active = new Set<string>();
  function visit(id: string) {
    if (active.has(id)) throw new Error("Dependency cycle: remove a circular prerequisite.");
    if (visited.has(id)) return;
    const row = byId.get(id);
    if (!row) throw new Error("A prerequisite is missing from this plan.");
    active.add(id); row.depends_on.forEach(visit); active.delete(id); visited.add(id);
  }
  rows.forEach(row => visit(row.followup_id));
  const changes: ActionChange[] = [];
  for (const row of rows) {
    const old = plan.actions.find(action => action.followup_id === row.followup_id);
    if (old && !editable(old)) continue;
    if (old && row.decision !== "keep") {
      changes.push({ action_id: row.followup_id, operation: row.decision, expected_revision: old.action_revision });
      continue;
    }
    if (!row.description.trim()) throw new Error("Every pending action needs a description.");
    const fields = { description: row.description.trim(), owner_side: row.owner_side,
      due_date: row.due_date || null, reminder_due_at: row.reminder_due_at || null,
      depends_on: [...row.depends_on].sort() };
    if (!old) changes.push({ action_id: row.followup_id, operation: "create", ...fields });
    else {
      const changed: ActionChange = { action_id: row.followup_id, operation: "update", expected_revision: old.action_revision };
      if (fields.description !== old.description) changed.description = fields.description;
      if (fields.owner_side !== old.owner_side) changed.owner_side = fields.owner_side;
      if (fields.due_date !== old.due_date) changed.due_date = fields.due_date;
      if (fields.reminder_due_at !== old.reminder_due_at) changed.reminder_due_at = fields.reminder_due_at;
      if (JSON.stringify(fields.depends_on) !== JSON.stringify([...old.depends_on].sort())) changed.depends_on = fields.depends_on;
      if (Object.keys(changed).length > 3) changes.push(changed);
    }
  }
  if (changes.length > 50) throw new Error("Submit at most 50 changed actions in one bundle.");
  return changes;
}
