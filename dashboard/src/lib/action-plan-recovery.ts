import type { AuthSession } from "./api";
import type { PlanBundle } from "./action-plan";

const PREFIX = "fix-action-plan-pending-v1:";
const MAX_SIZE = 250_000;
export function recoveryKey(session: AuthSession | null, projectId: string): string | null {
  if (!session?.authenticated || !session.user_id) return null;
  const boundary = session.session_id || (!session.auth_enabled ? "auth-disabled" : null);
  return boundary ? PREFIX + encodeURIComponent(JSON.stringify([session.user_id, boundary, projectId])) : null;
}
function validBundle(value: unknown): value is PlanBundle {
  if (!value || typeof value !== "object") return false;
  const b = value as PlanBundle;
  return typeof b.operation_id === "string" && b.operation_id.length > 0 && b.operation_id.length <= 120
    && Number.isInteger(b.expected_plan_revision) && b.expected_plan_revision >= 0
    && typeof b.expected_project_version === "string" && typeof b.reason === "string" && !!b.reason.trim()
    && b.reason.length <= 2000 && typeof b.replace_legacy_summary === "boolean"
    && (b.evidence_ref === undefined || typeof b.evidence_ref === "string")
    && (b.review_context === undefined || !!b.review_context && typeof b.review_context.review_id === "string"
      && !!b.review_context.review_id && typeof b.review_context.decision_id === "string" && !!b.review_context.decision_id
      && Number.isInteger(b.review_context.expected_revision) && b.review_context.expected_revision >= 0)
    && Array.isArray(b.actions) && b.actions.length > 0 && b.actions.length <= 50
    && b.actions.every(a => a && typeof a.action_id === "string" && a.action_id.length > 0 && a.action_id.length <= 200
      && ["create", "update", "complete", "cancel"].includes(a.operation)
      && (a.operation === "create" ? a.action_id.startsWith("new:") && typeof a.description === "string" && !!a.description.trim()
        : Number.isInteger(a.expected_revision) && (a.expected_revision ?? -1) >= 0)
      && (a.depends_on == null || Array.isArray(a.depends_on) && a.depends_on.length <= 50 && a.depends_on.every(id => typeof id === "string")));
}
export function readPendingBundle(key: string, projectId: string): PlanBundle | null {
  const raw = sessionStorage.getItem(key);
  if (raw === null) return null;
  if (raw.length > MAX_SIZE) throw new Error("Recovery record is too large; resolve it before creating another bundle.");
  let record;
  try { record = JSON.parse(raw); } catch { throw new Error("Unreadable pending submission. Do not create another bundle until its result is reconciled."); }
  if (!record || typeof record !== "object" || record.version !== 1 || record.project_id !== projectId || !validBundle(record.bundle))
    throw new Error("Invalid pending submission record. Resolve its receipt before creating another bundle.");
  return record.bundle;
}
export function savePendingBundle(key: string, projectId: string, bundle: PlanBundle): void {
  const old = readPendingBundle(key, projectId);
  if (old && JSON.stringify(old) !== JSON.stringify(bundle)) throw new Error("Another pending bundle must be reconciled first. Reload this plan.");
  const raw = JSON.stringify({ version: 1, project_id: projectId, bundle });
  if (raw.length > MAX_SIZE) throw new Error("This bundle exceeds the browser recovery limit.");
  sessionStorage.setItem(key, raw);
  if (sessionStorage.getItem(key) !== raw) throw new Error("The pending bundle could not be verified in browser storage.");
}
export function clearPendingBundle(key: string, projectId: string, operationId: string, reviewDecisionId?: string): boolean {
  try {
    const old = readPendingBundle(key, projectId);
    if (!old) return true;
    if (old.operation_id !== operationId || old.review_context?.decision_id !== reviewDecisionId) return false;
    sessionStorage.removeItem(key);
    return sessionStorage.getItem(key) === null;
  } catch { return false; }
}
