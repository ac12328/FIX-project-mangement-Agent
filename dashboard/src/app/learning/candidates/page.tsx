"use client";

import { useCallback, useEffect, useState } from "react";
import { FilterX, Inbox } from "lucide-react";
import { CandidateCard, CandidateAction, CANDIDATE_ACTION_LABELS } from "@/components/learning/candidate-card";
import { CandidateDetailPanel } from "@/components/learning/candidate-detail-panel";
import { LearningActionDialog } from "@/components/learning/learning-action-dialog";
import { LearningShell, LearningState } from "@/components/learning/learning-shell";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import {
  actOnLearningCandidate,
  candidateRevision,
  confirmation,
  getLearningCandidate,
  getLearningCandidates,
  learningErrorState,
  LearningCandidate,
  LearningCandidateDetail,
} from "@/lib/learning-api";
import { useEvents } from "@/lib/useEvents";

const PAGE_SIZE = 20;
const STATUS_OPTIONS = [
  ["all", "All statuses"],
  ["needs_review", "Needs review"],
  ["needs_revision", "Needs revision"],
  ["rejected", "Rejected"],
  ["quarantined", "Quarantined"],
] as const;
const RISK_OPTIONS = [
  ["all", "All risks"],
  ["low", "Low risk"],
  ["medium", "Medium risk"],
  ["high", "High risk"],
  ["critical", "Critical risk"],
] as const;
type PageState = "loading" | "ready" | "empty" | "error" | "forbidden" | "stale";
type DetailState = "loading" | "ready" | "error";

export default function CandidatesPage() {
  const [items, setItems] = useState<LearningCandidate[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [statusFilter, setStatusFilter] = useState("all");
  const [riskFilter, setRiskFilter] = useState("all");
  const [state, setState] = useState<PageState>("loading");
  const [actionTarget, setActionTarget] = useState<LearningCandidate | null>(null);
  const [action, setAction] = useState<CandidateAction>("approve");
  const [detailTarget, setDetailTarget] = useState<LearningCandidate | null>(null);
  const [detail, setDetail] = useState<LearningCandidateDetail | null>(null);
  const [detailState, setDetailState] = useState<DetailState>("loading");

  const load = useCallback(async () => {
    setState("loading");
    try {
      const data = await getLearningCandidates({
        status: statusFilter === "all" ? undefined : statusFilter,
        risk_level: riskFilter === "all" ? undefined : riskFilter,
        limit: PAGE_SIZE,
        offset,
      });
      if (!data.items.length && data.total > 0 && offset > 0) {
        const lastPageOffset = Math.floor((data.total - 1) / PAGE_SIZE) * PAGE_SIZE;
        setOffset(lastPageOffset);
        return;
      }
      setItems(data.items);
      setTotal(data.total);
      setState(data.items.length ? "ready" : "empty");
    } catch (error) {
      setState(learningErrorState(error));
    }
  }, [offset, riskFilter, statusFilter]);

  useEffect(() => {
    const timer = window.setTimeout(() => { void load(); }, 0);
    return () => window.clearTimeout(timer);
  }, [load]);
  useEvents({ eventTypes: ["learning_candidate_changed"], onRefresh: load });

  const openDetail = useCallback(async (candidate: LearningCandidate) => {
    setDetailTarget(candidate);
    setDetail(null);
    setDetailState("loading");
    try {
      setDetail(await getLearningCandidate(candidate.candidate_id, candidate.candidate_version));
      setDetailState("ready");
    } catch {
      setDetailState("error");
    }
  }, []);

  const closeDetail = useCallback(() => {
    setDetailTarget(null);
    setDetail(null);
  }, []);

  const chooseAction = (candidate: LearningCandidate, nextAction: CandidateAction) => {
    setActionTarget(candidate);
    setAction(nextAction);
  };

  const act = async (reason: string, confirmed: boolean, fields: Record<string, string>) => {
    if (!actionTarget) return;
    const revision = candidateRevision(actionTarget);
    const risky = actionTarget.scope_type === "global" || action === "merge" || ["high", "critical"].includes(actionTarget.risk_level ?? "");
    const confirmationFields = risky
      ? await confirmation(action, actionTarget.candidate_id, revision, {
        resource_version: actionTarget.candidate_version,
        scope: actionTarget.scope_type,
        risk: actionTarget.risk_level ?? "low",
        actor_id: actionTarget.operator_id,
        reason,
      })
      : { confirmed };
    await actOnLearningCandidate(actionTarget.candidate_id, actionTarget.candidate_version, {
      action,
      reason,
      idempotency_key: crypto.randomUUID(),
      ...confirmationFields,
      proposed_value: fields.proposed_value ? JSON.parse(fields.proposed_value) : undefined,
      scope_type: fields.scope_type,
      scope_id: fields.scope_id,
      suggested_expiry: fields.suggested_expiry,
      merge_candidate_id: fields.candidate_id,
      merge_candidate_version: fields.candidate_version ? Number(fields.candidate_version) : undefined,
    });
    await load();
  };

  const fields = action === "edit_and_approve"
    ? [{ name: "proposed_value", label: "Proposed value JSON", required: true }]
    : action === "narrow_scope"
      ? [{ name: "scope_type", label: "Scope type", required: true }, { name: "scope_id", label: "Scope ID", required: true }]
      : action === "merge"
        ? [{ name: "candidate_id", label: "Equivalent candidate ID", required: true }, { name: "candidate_version", label: "Candidate version", required: true }]
        : [];
  const hasFilters = statusFilter !== "all" || riskFilter !== "all";
  const showingStart = total ? offset + 1 : 0;
  const showingEnd = Math.min(offset + items.length, total);
  const currentPage = Math.floor(offset / PAGE_SIZE) + 1;
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));

  const clearFilters = () => {
    setStatusFilter("all");
    setRiskFilter("all");
    setOffset(0);
  };

  return <LearningShell title="Candidates" description="Review candidate claims in a readable queue with governed decisions and complete provenance." active="/learning/candidates">
    <Card>
      <CardContent className="flex flex-col gap-4 p-4 sm:flex-row sm:items-end sm:justify-between">
        <div className="grid flex-1 gap-3 sm:grid-cols-2 sm:items-end">
          <label className="block text-sm font-medium" htmlFor="candidate-status-filter">
            Status
            <Select value={statusFilter} onValueChange={(value) => { if (value) { setStatusFilter(value); setOffset(0); } }}>
              <SelectTrigger id="candidate-status-filter" className="mt-1 h-11"><SelectValue /></SelectTrigger>
              <SelectContent>{STATUS_OPTIONS.map(([value, label]) => <SelectItem key={value} value={value}>{label}</SelectItem>)}</SelectContent>
            </Select>
          </label>
          <label className="block text-sm font-medium" htmlFor="candidate-risk-filter">
            Risk
            <Select value={riskFilter} onValueChange={(value) => { if (value) { setRiskFilter(value); setOffset(0); } }}>
              <SelectTrigger id="candidate-risk-filter" className="mt-1 h-11"><SelectValue /></SelectTrigger>
              <SelectContent>{RISK_OPTIONS.map(([value, label]) => <SelectItem key={value} value={value}>{label}</SelectItem>)}</SelectContent>
            </Select>
          </label>
        </div>
        <Button variant="outline" disabled={!hasFilters} onClick={clearFilters}><FilterX aria-hidden="true" />Clear filters</Button>
      </CardContent>
    </Card>

    {state === "ready" && <div className="space-y-4" aria-label="Candidate review queue">
      {items.map((candidate) => <CandidateCard
        key={`${candidate.candidate_id}-${candidate.candidate_version}`}
        candidate={candidate}
        onAction={chooseAction}
        onViewEvidence={(item) => { void openDetail(item); }}
      />)}
    </div>}

    {state === "empty" && hasFilters && <Card><CardContent className="flex flex-col items-center gap-3 p-8 text-center">
      <Inbox className="size-6 text-muted-foreground" aria-hidden="true" />
      <div><h2 className="font-semibold">No matching candidates</h2><p className="mt-1 text-sm text-muted-foreground">Try a different status or risk level.</p></div>
      <Button variant="outline" onClick={clearFilters}>Clear filters</Button>
    </CardContent></Card>}
    {state === "empty" && !hasFilters && <LearningState state="empty" onRetry={load} />}
    {state !== "ready" && state !== "empty" && <LearningState state={state} onRetry={load} />}

    {state === "ready" && <nav aria-label="Candidate pagination" className="flex flex-col gap-3 rounded-xl border bg-card px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
      <p className="text-sm text-muted-foreground">Showing {showingStart}–{showingEnd} of {total} candidates</p>
      <div className="flex items-center justify-between gap-2 sm:justify-end">
        <span className="mr-1 text-sm text-muted-foreground">Page {currentPage} of {pageCount}</span>
        <Button variant="outline" size="sm" disabled={offset === 0} onClick={() => setOffset((current) => Math.max(0, current - PAGE_SIZE))}>Previous</Button>
        <Button variant="outline" size="sm" disabled={offset + PAGE_SIZE >= total} onClick={() => setOffset((current) => current + PAGE_SIZE)}>Next</Button>
      </div>
    </nav>}

    <LearningActionDialog
      open={Boolean(actionTarget)}
      title={`${CANDIDATE_ACTION_LABELS[action]} candidate`}
      description="The action stays open on domain errors. High-risk controls are bound to the current validation revision."
      highRisk={action === "merge" || ((actionTarget?.risk_level === "high" || actionTarget?.risk_level === "critical") && action === "approve")}
      fields={fields}
      onClose={() => setActionTarget(null)}
      onConfirm={act}
    />

    {detailTarget && <CandidateDetailPanel
      candidate={detailTarget}
      detail={detail}
      state={detailState}
      onClose={closeDetail}
      onRetry={() => { void openDetail(detailTarget); }}
    />}
  </LearningShell>;
}
