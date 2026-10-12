"use client";

import { AlertTriangle, CheckCircle2, CircleSlash2, Database, FileText, ShieldAlert } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { LearningDetailPanel } from "@/components/learning/learning-detail-panel";
import { LearningCandidate, LearningCandidateDetail, LearningRecord } from "@/lib/learning-api";

function humanize(value: string) {
  return value.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function isRecord(value: unknown): value is LearningRecord {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function parseJson(value: unknown): unknown {
  if (typeof value !== "string") return value;
  const trimmed = value.trim();
  if (!trimmed.startsWith("{") && !trimmed.startsWith("[")) return value;
  try { return JSON.parse(trimmed); } catch { return value; }
}

function formatValue(value: unknown, key?: string) {
  if (value === null || value === undefined || value === "") return "Not available";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "number" && key && (key === "passed" || key.startsWith("requires_") || key.endsWith("_blocked"))) return value ? "Yes" : "No";
  if (typeof value === "string" && key && /(at|date|from|to|expiry)$/.test(key)) {
    const date = new Date(value);
    if (!Number.isNaN(date.valueOf())) return date.toLocaleString();
  }
  return String(value);
}

function StructuredValue({ value, valueKey }: { value: unknown; valueKey?: string }) {
  const parsed = parseJson(value);
  if (Array.isArray(parsed) || isRecord(parsed)) {
    return <pre className="rounded-lg border bg-muted/40 p-3 font-mono text-xs leading-5 text-foreground">{JSON.stringify(parsed, null, 2)}</pre>;
  }
  return <span className="whitespace-pre-wrap break-words [overflow-wrap:anywhere]">{formatValue(parsed, valueKey)}</span>;
}

function SummaryGrid({ rows }: { rows: Array<[string, unknown, string?]> }) {
  return <dl className="grid gap-4 sm:grid-cols-2">
    {rows.map(([label, value, key]) => <div key={label} className="min-w-0">
      <dt className="text-xs font-medium uppercase tracking-wide text-muted-foreground">{label}</dt>
      <dd className="mt-1 break-words text-sm font-medium [overflow-wrap:anywhere]"><StructuredValue value={value} valueKey={key} /></dd>
    </div>)}
  </dl>;
}

function PanelSection({ title, icon: Icon, children }: {
  title: string;
  icon: typeof FileText;
  children: React.ReactNode;
}) {
  return <section className="min-w-0 rounded-xl border bg-card p-4 sm:p-5">
    <div className="mb-4 flex items-center gap-2">
      <Icon className="size-4 text-primary" aria-hidden="true" />
      <h3 className="font-semibold">{title}</h3>
    </div>
    {children}
  </section>;
}

function RecordCard({ record, index }: { record: LearningRecord; index: number }) {
  const entries = Object.entries(record);
  const identifier = entries.find(([key]) => key === "id" || key.endsWith("_id"))?.[1];
  return <article className="min-w-0 rounded-lg border bg-background p-3">
    <h4 className="break-all font-mono text-xs font-medium text-muted-foreground">{identifier ? String(identifier) : `Record ${index + 1}`}</h4>
    <dl className="mt-3 grid gap-3 sm:grid-cols-2">
      {entries.map(([key, value]) => <div key={key} className="min-w-0">
        <dt className="text-xs font-medium text-muted-foreground">{humanize(key)}</dt>
        <dd className="mt-1 break-words text-sm [overflow-wrap:anywhere]"><StructuredValue value={value} valueKey={key} /></dd>
      </div>)}
    </dl>
  </article>;
}

function HistoryDisclosure({ title, items }: { title: string; items: LearningRecord[] }) {
  return <details className="group min-w-0 rounded-xl border bg-card">
    <summary className="flex min-h-12 cursor-pointer list-none items-center justify-between gap-4 px-4 py-3 font-medium outline-none focus-visible:ring-2 focus-visible:ring-ring [&::-webkit-details-marker]:hidden">
      <span>{title}</span>
      <span className="rounded-full border bg-muted px-2 py-0.5 text-xs text-muted-foreground">{items.length}</span>
    </summary>
    <div className="space-y-3 border-t p-4">
      {items.length ? items.map((item, index) => <RecordCard key={`${title}-${index}`} record={item} index={index} />) : <p className="text-sm text-muted-foreground">No {title.toLowerCase()} recorded.</p>}
    </div>
  </details>;
}

function EvidenceSummary({ detail }: { detail: LearningCandidateDetail }) {
  const source = detail.observation;
  return <PanelSection title="Source and evidence" icon={FileText}>
    {source ? <div className="space-y-4">
      <SummaryGrid rows={[
        ["Source type", source.source_type, "source_type"],
        ["Source ID", source.source_id, "source_id"],
        ["Project", source.project_id, "project_id"],
        ["Contact", source.contact_id, "contact_id"],
        ["Occurred", source.occurred_at, "occurred_at"],
        ["Trust tier", source.trust_tier, "trust_tier"],
        ["Sensitivity", source.sensitivity, "sensitivity"],
        ["Content hash", source.content_hash, "content_hash"],
      ]} />
      {source.normalized_text ? <div>
        <h4 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Normalized source text</h4>
        <blockquote className="mt-2 whitespace-pre-wrap break-words rounded-lg border-l-4 border-primary bg-muted/40 p-4 text-sm leading-6 [overflow-wrap:anywhere]">{String(source.normalized_text)}</blockquote>
      </div> : null}
    </div> : <p className="text-sm text-muted-foreground">No source observation is attached.</p>}

    {detail.evidence.length > 0 && <details className="mt-4 rounded-lg border bg-background">
      <summary className="cursor-pointer px-3 py-3 text-sm font-medium outline-none focus-visible:ring-2 focus-visible:ring-ring">Linked evidence records ({detail.evidence.length})</summary>
      <div className="space-y-3 border-t p-3">{detail.evidence.map((item, index) => <RecordCard key={`evidence-${index}`} record={item} index={index} />)}</div>
    </details>}
  </PanelSection>;
}

function ValidationSummary({ detail }: { detail: LearningCandidateDetail }) {
  const validation = detail.validation;
  const evaluation = detail.evaluation;
  return <div className="space-y-4">
    <PanelSection title="Validation summary" icon={ShieldAlert}>
      {validation ? <div className="space-y-4">
        <SummaryGrid rows={[
          ["Result", validation.passed, "passed"],
          ["Disposition", validation.disposition, "disposition"],
          ["Risk", validation.risk_level, "risk_level"],
          ["Risk score", validation.risk_score, "risk_score"],
          ["Requires review", validation.requires_review, "requires_review"],
          ["Requires evaluation", validation.requires_evaluation, "requires_evaluation"],
          ["Validator", validation.validator_version, "validator_version"],
          ["Policy", validation.policy_version, "policy_version"],
          ["Validated", validation.validation_at, "validation_at"],
          ["Validation ID", validation.validation_id, "validation_id"],
        ]} />
        <div><h4 className="mb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">Findings</h4><StructuredValue value={validation.findings_json ?? []} valueKey="findings_json" /></div>
      </div> : <p className="text-sm text-muted-foreground">This candidate has not been validated.</p>}
    </PanelSection>

    <PanelSection title="Evaluation summary" icon={validation?.passed ? CheckCircle2 : CircleSlash2}>
      {evaluation ? <div className="space-y-4">
        <SummaryGrid rows={[
          ["Result", evaluation.passed, "passed"],
          ["Runner", evaluation.runner_version, "runner_version"],
          ["Policy", evaluation.policy_version, "policy_version"],
          ["Evaluated", evaluation.evaluated_at, "evaluated_at"],
          ["Evaluation ID", evaluation.evaluation_id, "evaluation_id"],
        ]} />
        <div><h4 className="mb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">Scores</h4><StructuredValue value={evaluation.scores_json ?? {}} valueKey="scores_json" /></div>
        <div><h4 className="mb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">Failure codes</h4><StructuredValue value={evaluation.failure_codes_json ?? []} valueKey="failure_codes_json" /></div>
      </div> : <p className="text-sm text-muted-foreground">No evaluation run is attached to this candidate.</p>}
    </PanelSection>
  </div>;
}

export function CandidateDetailPanel({ candidate, detail, state, onClose, onRetry }: {
  candidate: LearningCandidate;
  detail: LearningCandidateDetail | null;
  state: "loading" | "ready" | "error";
  onClose: () => void;
  onRetry: () => void;
}) {
  return <LearningDetailPanel
    title="Candidate provenance"
    description={`${humanize(candidate.predicate)} · ${candidate.candidate_id} v${candidate.candidate_version}`}
    onClose={onClose}
  >
    {state === "loading" && <div aria-label="Loading candidate provenance" className="space-y-3">
      <Skeleton className="h-32 w-full" /><Skeleton className="h-48 w-full" /><Skeleton className="h-40 w-full" />
    </div>}

    {state === "error" && <Card><CardContent className="flex flex-col items-center gap-3 p-8 text-center">
      <AlertTriangle className="size-6 text-muted-foreground" aria-hidden="true" />
      <div><h3 className="font-semibold">Could not load provenance</h3><p className="mt-1 text-sm text-muted-foreground">The candidate detail request failed.</p></div>
      <Button variant="outline" onClick={onRetry}>Try again</Button>
    </CardContent></Card>}

    {state === "ready" && detail && <>
      <PanelSection title="Candidate summary" icon={Database}>
        <div className="space-y-4">
          <SummaryGrid rows={[
            ["Target", detail.candidate.target_type, "target_type"],
            ["Subject", [detail.candidate.subject_type, detail.candidate.subject_id].filter(Boolean).join(" · ") || "Not specified"],
            ["Predicate", detail.candidate.predicate, "predicate"],
            ["Scope", [detail.candidate.scope_type, detail.candidate.scope_id].filter(Boolean).join(" · ")],
            ["Confidence", `${Math.round(Number(detail.candidate.confidence) * 100)}%`],
            ["Disposition", candidate.disposition ?? "Unreviewed"],
            ["Risk", candidate.risk_level ?? "Unknown"],
            ["Suggested expiry", detail.candidate.suggested_expiry, "suggested_expiry"],
          ]} />
          <div><h4 className="mb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">Proposed value</h4><StructuredValue value={detail.candidate.proposed_value} /></div>
          {detail.candidate.reasoning_summary ? <div><h4 className="mb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">Reasoning</h4><p className="whitespace-pre-wrap break-words text-sm leading-6 [overflow-wrap:anywhere]">{String(detail.candidate.reasoning_summary)}</p></div> : null}
        </div>
      </PanelSection>

      <EvidenceSummary detail={detail} />
      <ValidationSummary detail={detail} />

      {detail.decision && <PanelSection title="Latest governance decision" icon={CheckCircle2}>
        <SummaryGrid rows={[
          ["Decision", detail.decision.decision_type, "decision_type"],
          ["Actor", detail.decision.actor_id, "actor_id"],
          ["Reason", detail.decision.reason, "reason"],
          ["Decided", detail.decision.decided_at, "decided_at"],
          ["Decision ID", detail.decision.decision_id, "decision_id"],
          ["Evaluation ID", detail.decision.evaluation_id, "evaluation_id"],
        ]} />
      </PanelSection>}

      {detail.fact && <PanelSection title="Materialized fact" icon={Database}><RecordCard record={detail.fact} index={0} /></PanelSection>}

      <div className="space-y-3" aria-label="Candidate history">
        <h3 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">History and downstream usage</h3>
        <HistoryDisclosure title="Decision history" items={detail.decisions} />
        <HistoryDisclosure title="Fact versions" items={detail.fact_versions} />
        <HistoryDisclosure title="Exposures" items={detail.exposures} />
        <HistoryDisclosure title="Outcomes" items={detail.outcomes} />
        <HistoryDisclosure title="Conflicts" items={detail.conflicts} />
        <HistoryDisclosure title="Publications" items={detail.publications} />
        <HistoryDisclosure title="Lifecycle events" items={detail.lifecycle} />
      </div>

      <details className="min-w-0 rounded-xl border bg-card">
        <summary className="min-h-12 cursor-pointer px-4 py-3 font-medium outline-none focus-visible:ring-2 focus-visible:ring-ring">Raw provenance data</summary>
        <pre className="border-t bg-muted/30 p-4 font-mono text-xs leading-5 text-muted-foreground">{JSON.stringify(detail, null, 2)}</pre>
      </details>
    </>}
  </LearningDetailPanel>;
}
