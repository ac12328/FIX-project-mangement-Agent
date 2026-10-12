"use client";

import { Menu } from "@base-ui/react/menu";
import { ChevronDown, Eye, ShieldCheck, XCircle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardFooter, CardHeader } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import { LearningCandidate } from "@/lib/learning-api";

export type CandidateAction = "approve" | "reject" | "edit_and_approve" | "narrow_scope" | "request_evidence" | "merge" | "quarantine";

export const CANDIDATE_ACTION_LABELS: Record<CandidateAction, string> = {
  approve: "Approve",
  reject: "Reject",
  edit_and_approve: "Edit & approve",
  narrow_scope: "Narrow scope",
  request_evidence: "Request evidence",
  merge: "Merge",
  quarantine: "Quarantine",
};

const MORE_ACTIONS: CandidateAction[] = ["edit_and_approve", "narrow_scope", "request_evidence", "merge", "quarantine"];

function humanize(value: string | null | undefined, fallback = "Not set") {
  if (!value) return fallback;
  return value.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function proposedValueText(value: unknown) {
  if (typeof value === "string") return value;
  if (value === null || value === undefined) return "No proposed value";
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return JSON.stringify(value, null, 2);
}

function formatDate(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString();
}

function CandidatePill({ value, emphasis = false }: { value: string; emphasis?: boolean }) {
  return <span className={cn(
    "inline-flex min-h-7 items-center rounded-full border px-2.5 py-1 text-xs font-medium",
    emphasis ? "border-primary/30 bg-primary/10 text-primary" : "bg-muted text-muted-foreground",
  )}>{humanize(value)}</span>;
}

function ProposedValue({ value }: { value: unknown }) {
  const text = proposedValueText(value);
  const isLong = text.length > 420;
  const contentClass = "whitespace-pre-wrap break-words font-mono text-sm leading-6 text-foreground [overflow-wrap:anywhere]";

  if (!isLong) return <pre className={contentClass}>{text}</pre>;
  return <div className="space-y-3">
    <pre className={contentClass}>{text.slice(0, 420)}…</pre>
    <details className="group rounded-lg border bg-background p-3">
      <summary className="cursor-pointer font-medium text-primary outline-none focus-visible:ring-2 focus-visible:ring-ring">Show full proposed value</summary>
      <pre className={cn(contentClass, "mt-3 border-t pt-3")}>{text}</pre>
    </details>
  </div>;
}

export function CandidateCard({
  candidate,
  onAction,
  onViewEvidence,
}: {
  candidate: LearningCandidate;
  onAction: (candidate: LearningCandidate, action: CandidateAction) => void;
  onViewEvidence: (candidate: LearningCandidate) => void;
}) {
  const canApprove = Boolean(candidate.passed) && candidate.disposition === "needs_review";
  const approvalReason = !Boolean(candidate.passed)
    ? "Approval is unavailable until validation passes."
    : candidate.disposition !== "needs_review"
      ? "Approval is only available for candidates that need review."
      : undefined;
  const subject = [candidate.subject_type, candidate.subject_id].filter(Boolean).join(" · ");
  const scope = candidate.scope_id ? `${humanize(candidate.scope_type)} · ${candidate.scope_id}` : humanize(candidate.scope_type);
  const evidenceCount = candidate.evidence_refs?.length ?? 0;

  return <Card className="gap-0 py-0">
    <CardHeader className="gap-3 border-b px-4 py-4 sm:px-5">
      <div className="min-w-0">
        <p className="text-xs font-medium uppercase tracking-wider text-muted-foreground">{humanize(candidate.target_type, "Learning candidate")}</p>
        <h2 className="mt-1 break-words text-lg font-semibold leading-snug [overflow-wrap:anywhere]">{humanize(candidate.predicate)}</h2>
        <p className="mt-1 break-all font-mono text-xs text-muted-foreground">{candidate.candidate_id} · v{candidate.candidate_version}</p>
      </div>
      <div className="flex flex-wrap gap-2 sm:justify-end">
        <CandidatePill value={candidate.disposition ?? "unreviewed"} emphasis={candidate.disposition === "needs_review"} />
        <CandidatePill value={`${candidate.risk_level ?? "unknown"} risk`} emphasis={candidate.risk_level === "high" || candidate.risk_level === "critical"} />
      </div>
    </CardHeader>

    <CardContent className="space-y-5 px-4 py-5 sm:px-5">
      <section aria-labelledby={`claim-${candidate.candidate_id}-${candidate.candidate_version}`}>
        <h3 id={`claim-${candidate.candidate_id}-${candidate.candidate_version}`} className="text-sm font-semibold">Proposed value</h3>
        <div className="mt-2 rounded-xl border bg-muted/40 p-4"><ProposedValue value={candidate.proposed_value} /></div>
      </section>

      {candidate.reasoning_summary && <section>
        <h3 className="text-sm font-semibold">Why it was proposed</h3>
        <p className="mt-1 whitespace-pre-wrap break-words text-sm leading-6 text-muted-foreground [overflow-wrap:anywhere]">{candidate.reasoning_summary}</p>
      </section>}

      <dl className="grid gap-4 rounded-xl border bg-background p-4 sm:grid-cols-2 lg:grid-cols-4">
        <div className="min-w-0"><dt className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Subject</dt><dd className="mt-1 break-words font-medium [overflow-wrap:anywhere]">{subject || "Not specified"}</dd></div>
        <div className="min-w-0"><dt className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Scope</dt><dd className="mt-1 break-words font-medium [overflow-wrap:anywhere]">{scope}</dd></div>
        <div><dt className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Confidence</dt><dd className="mt-1 font-medium">{Math.round(candidate.confidence * 100)}%</dd></div>
        <div><dt className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Evidence</dt><dd className="mt-1 font-medium">{evidenceCount} reference{evidenceCount === 1 ? "" : "s"}</dd></div>
      </dl>

      <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
        <span>Created {formatDate(candidate.created_at)}</span>
        <span>{candidate.validation_id ? `Validation ${candidate.validation_id}` : "No validation attached"}</span>
      </div>
    </CardContent>

    <CardFooter className="flex flex-col items-stretch gap-3 border-t bg-muted/20 px-4 py-4 sm:flex-row sm:flex-wrap sm:items-center sm:px-5">
      <div className="flex flex-1 flex-wrap gap-2">
        <Button size="sm" disabled={!canApprove} title={approvalReason} onClick={() => onAction(candidate, "approve")}>
          <ShieldCheck aria-hidden="true" />Approve
        </Button>
        <Button size="sm" variant="destructive" onClick={() => onAction(candidate, "reject")}>
          <XCircle aria-hidden="true" />Reject
        </Button>
        <Button size="sm" variant="outline" onClick={() => onViewEvidence(candidate)}>
          <Eye aria-hidden="true" />View evidence
        </Button>
      </div>

      <Menu.Root>
        <Menu.Trigger className="inline-flex h-10 min-w-32 items-center justify-center gap-2 rounded-lg border bg-background px-3 text-sm font-medium outline-none transition-colors hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring aria-expanded:bg-muted">
          More actions<ChevronDown className="size-4" aria-hidden="true" />
        </Menu.Trigger>
        <Menu.Portal>
          <Menu.Positioner align="end" sideOffset={8} className="z-50">
            <Menu.Popup aria-label={`More actions for ${candidate.candidate_id}`} className="min-w-52 rounded-lg border bg-popover p-1 text-popover-foreground shadow-lg outline-none data-[starting-style]:opacity-0 data-[ending-style]:opacity-0">
              {MORE_ACTIONS.map((action) => <Menu.Item
                key={action}
                className={cn(
                  "flex min-h-10 cursor-default items-center rounded-md px-3 text-sm outline-none data-[highlighted]:bg-accent data-[highlighted]:text-accent-foreground",
                  action === "quarantine" && "text-destructive data-[highlighted]:bg-destructive/10 data-[highlighted]:text-destructive",
                )}
                onClick={() => onAction(candidate, action)}
              >{CANDIDATE_ACTION_LABELS[action]}</Menu.Item>)}
            </Menu.Popup>
          </Menu.Positioner>
        </Menu.Portal>
      </Menu.Root>

      {!canApprove && <p className="w-full text-xs text-muted-foreground">{approvalReason}</p>}
    </CardFooter>
  </Card>;
}
