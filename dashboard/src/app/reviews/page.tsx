"use client";

import { Suspense, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import {
  ArrowRight,
  CheckCircle2,
  ChevronDown,
  ClipboardCheck,
  ExternalLink,
  FileText,
  Mail,
  Search,
  ShieldAlert,
  X,
  XCircle,
} from "lucide-react";

import { NotesChangeComparison } from "@/components/reviews/notes-change";
import { PageHeader } from "@/components/page-header";
import { PageToolbar, ToolbarField } from "@/components/page-toolbar";
import { Pagination } from "@/components/pagination";
import { ErrorPanel, LoadingPanel, StatePanel } from "@/components/state-panel";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { reviewAction } from "@/lib/api";
import { notifyError, notifySuccess, notifyWarning } from "@/lib/notifications";
import { reviewActionFeedback } from "@/lib/review-action-feedback";
import {
  collectReviewCitations,
  isSafeReviewUrl,
  parseReviewAssessment,
  parseReviewChanges,
  safeReviewText,
  sanitizeReviewHeader,
} from "@/lib/review-sanitization";
import type {
  ReviewAssessment,
  ReviewChange,
  ReviewCitation,
  ReviewItem,
} from "@/lib/types";
import { formatTimestamp } from "@/lib/ui-format";
import { getUiReviews, uiV2Keys } from "@/lib/ui-v2-api";
import type { UiReviewFilters, UiReviewItem } from "@/lib/ui-v2-types";
import { pageFromSearchParam, updateSearchParams } from "@/lib/url-state";
import { cn } from "@/lib/utils";

const PAGE_SIZE = 20;

const REJECT_TAGS = [
  "Wrong milestone",
  "Wrong ball-with",
  "Insufficient evidence",
  "Wrong project status",
  "Other",
];

const PROTECTED_FIELDS = new Set(["current_milestone", "project_status", "ball_with"]);

const FIELD_LABELS: Record<string, string> = {
  ball_with: "Ball with",
  client_name: "Client name",
  current_milestone: "Current milestone",
  next_action: "Next action",
  next_action_due: "Next action due",
  next_action_owner: "Next action owner",
  notes: "Notes",
  project_status: "Project status",
};

export default function ReviewsPage() {
  return <Suspense fallback={<LoadingPanel label="Loading review queue" />}><ReviewsContent /></Suspense>;
}

function ReviewsContent() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const filters: UiReviewFilters = {
    q: searchParams.get("q") || undefined,
    risk: searchParams.get("risk") || undefined,
    page: pageFromSearchParam(searchParams.get("page")),
    page_size: PAGE_SIZE,
  };
  const query = useQuery({ queryKey: uiV2Keys.reviews(filters), queryFn: () => getUiReviews(filters) });
  const data = query.data;
  const reviews = (data?.items ?? []).map(toReviewItem);

  function replace(updates: Record<string, string | number | null | undefined>) {
    const next = updateSearchParams(searchParams, { ...updates, page: updates.page ?? null });
    router.replace(`${pathname}${next ? `?${next}` : ""}`, { scroll: false });
  }

  async function handleAction(
    updateId: string,
    action: "approve" | "reject",
    rejectTags?: string[],
    rejectNote?: string,
  ) {
    try {
      const rejectPayload = action === "reject"
        ? { reject_tags: rejectTags ?? [], reject_reason: rejectNote || undefined }
        : undefined;
      const result = await reviewAction(updateId, action, rejectPayload);
      const feedback = reviewActionFeedback(action, result, null);
      if (feedback.tone === "success") notifySuccess(feedback.title, feedback.description);
      else if (feedback.tone === "warning") notifyWarning(feedback.title, feedback.description);
      else notifyError(new Error(feedback.description), feedback.title);
      if (feedback.refetch) await query.refetch();
    } catch (error) {
      const feedback = reviewActionFeedback(action, null, error);
      if (feedback.tone === "warning") notifyWarning(feedback.title, feedback.description);
      else notifyError(error, feedback.description);
      if (feedback.refetch) await query.refetch();
    }
  }

  return (
    <div className="mx-auto max-w-6xl space-y-6">
      <PageHeader
        eyebrow="Work"
        title="Pending reviews"
        description="Compare proposed changes with current project truth and attributable evidence before applying them."
        metadata={data ? <>{data.total} pending reviews <span aria-hidden="true">•</span> Updated {formatTimestamp(data.generated_at)}</> : undefined}
      />

      <PageToolbar summary={data ? `${data.total} matching reviews` : "Loading review count"}>
        <form
          className="flex min-w-0 flex-1 gap-2 sm:max-w-md"
          onSubmit={(event) => {
            event.preventDefault();
            const value = new FormData(event.currentTarget).get("q")?.toString().trim();
            replace({ q: value || null });
          }}
        >
          <ToolbarField label="Search reviews" className="min-w-0 flex-1">
            <Input key={filters.q ?? ""} name="q" defaultValue={filters.q} placeholder="Project, client, task, or proposed value" />
          </ToolbarField>
          <Button type="submit" variant="outline" className="mt-[1.375rem]" aria-label="Search reviews"><Search className="size-4" /></Button>
        </form>
        <ToolbarField label="Risk" className="sm:w-40">
          <Select value={filters.risk ?? "all"} onValueChange={(value) => replace({ risk: value === "all" ? null : value })}>
            <SelectTrigger><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All risk</SelectItem>
              <SelectItem value="high">High</SelectItem>
              <SelectItem value="medium">Medium</SelectItem>
              <SelectItem value="low">Low</SelectItem>
            </SelectContent>
          </Select>
        </ToolbarField>
        {filters.q || filters.risk ? <Button variant="ghost" onClick={() => router.replace(pathname)}><X className="size-4" />Clear</Button> : null}
      </PageToolbar>

      {query.isPending ? <ReviewsSkeleton /> : null}
      {query.isError ? <ErrorPanel description={query.error instanceof Error ? query.error.message : "Reviews could not be loaded."} onRetry={() => void query.refetch()} /> : null}
      {data && reviews.length === 0 ? (
        <StatePanel
          icon={ClipboardCheck}
          title="No reviews waiting"
          description="Approval requests will appear here when the system flags a change that needs confirmation."
        />
      ) : null}
      {data && reviews.length > 0 ? (
        <section className="space-y-5" aria-label="Pending reviews">
          {reviews.map((review) => <ReviewCard key={review.update_id} review={review} onAction={handleAction} />)}
          <div className="overflow-hidden rounded-xl border bg-card">
            <Pagination page={data.page} pageSize={data.page_size} total={data.total} onPageChange={(page) => replace({ page })} />
          </div>
        </section>
      ) : null}
    </div>
  );
}

function toReviewItem(item: UiReviewItem): ReviewItem {
  return {
    update_id: item.update_id,
    project_id: item.project_id,
    proposed_changes: item.proposed_changes,
    task_type: item.task_type ?? null,
    confidence: item.confidence,
    risk_level: item.risk_level ?? null,
    created_at: item.created_at ?? null,
    client_name: item.project_label ?? null,
    assessment_json: item.assessment_json ?? null,
    policy_version: item.policy_version ?? null,
    source_type: item.source_type ?? null,
    source_ref: item.source_ref ?? null,
  };
}

function ReviewCard({
  review,
  onAction,
}: {
  review: ReviewItem;
  onAction: (
    id: string,
    action: "approve" | "reject",
    rejectTags?: string[],
    rejectNote?: string,
  ) => Promise<void>;
}) {
  const [rejecting, setRejecting] = useState(false);
  const [selectedTags, setSelectedTags] = useState<string[]>([]);
  const [rejectNote, setRejectNote] = useState("");
  const [submitting, setSubmitting] = useState<"approve" | "reject" | null>(null);

  const changes: ReviewChange[] = parseReviewChanges(review.proposed_changes);
  const assessment = parseReviewAssessment(review.assessment_json);
  const citations = collectReviewCitations(assessment, changes, review.source_ref);
  const policyVersion = safeReviewText(review.policy_version, 120);
  const header = sanitizeReviewHeader(review);
  const criteria = assessment?.milestone_criteria ?? [];
  const decision = assessmentDecision(assessment);
  const confPct = Number.isFinite(review.confidence) ? Math.round(review.confidence * 100) : null;
  const accentBorder = riskBorder(header.riskLevel ?? null, decision);
  const allReasons = uniqueReasons(changes);
  const reasons = allReasons.filter((reason) => !isSourceOnlyReason(reason));
  const sourceSummary = buildSourceSummary(review, allReasons, citations);
  const protectedChange = changes.some((change) => PROTECTED_FIELDS.has(change.field));
  const reviewLabel = changes.length === 1
    ? `Update ${formatFieldLabel(changes[0].field)}`
    : `${changes.length} proposed changes`;

  const toggleTag = (tag: string) =>
    setSelectedTags((prev) =>
      prev.includes(tag) ? prev.filter((item) => item !== tag) : [...prev, tag],
    );

  const cancelReject = () => {
    setRejecting(false);
    setSelectedTags([]);
    setRejectNote("");
  };

  const runAction = async (action: "approve" | "reject") => {
    setSubmitting(action);
    try {
      await onAction(
        review.update_id,
        action,
        action === "reject" ? selectedTags : undefined,
        action === "reject" ? rejectNote : undefined,
      );
    } catch {
      // handleAction already toasts; never leak into unhandledRejection
    } finally {
      setSubmitting(null);
    }
  };

  return (
    <Card
      className={cn(
        "overflow-hidden border border-border/80 border-l-4 bg-card shadow-sm",
        accentBorder,
      )}
    >
      <CardHeader className="border-b border-border/60 px-5 py-5 sm:px-6">
        <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <Badge
                variant="outline"
                className="border-warning/35 bg-warning/10 text-xs font-semibold text-warning"
              >
                Pending update
              </Badge>
              {header.riskLevel && (
                <span className={cn("rounded-full border px-2.5 py-0.5 text-xs font-semibold", riskTone(header.riskLevel))}>
                  {formatFieldLabel(header.riskLevel)} risk
                </span>
              )}
            </div>

            <h2 className="mt-3 text-lg font-semibold leading-tight text-foreground">
              {reviewLabel}
            </h2>
            <div className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-sm">
              <Link
                href={`/projects/${review.project_id}`}
                className="font-mono font-semibold text-primary underline-offset-4 hover:underline"
              >
                {header.projectLabel}
              </Link>
              {header.clientName && (
                <>
                  <span className="text-border" aria-hidden="true">/</span>
                  <span className="font-medium text-foreground/80">{header.clientName}</span>
                </>
              )}
            </div>
          </div>

          <div className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground sm:justify-end">
            <span>{changes.length} change{changes.length === 1 ? "" : "s"}</span>
            {review.created_at && <span>{formatReviewDate(review.created_at)}</span>}
          </div>
        </div>
      </CardHeader>

      <CardContent className="space-y-5 px-5 py-5 sm:px-6 sm:py-6">
        <section aria-labelledby={`changes-${review.update_id}`}>
          <div className="mb-3 flex items-center justify-between gap-3">
            <h3 id={`changes-${review.update_id}`} className="text-xs font-bold uppercase tracking-[0.12em] text-foreground/70">
              Proposed changes
            </h3>
            <span className="text-xs text-muted-foreground">Current to proposed</span>
          </div>

          {changes.length > 0 ? (
            <div className="space-y-3">
              {changes.map((change, index) => (
                <ChangeComparison key={`${change.field}-${index}`} change={change} />
              ))}
            </div>
          ) : (
            <div className="rounded-xl border border-warning/30 bg-warning/10 p-4 text-sm text-foreground">
              No valid field changes were supplied. Reject this request or inspect the review details.
            </div>
          )}
        </section>

        {reasons.length > 0 && (
          <section className="rounded-xl border border-border/70 bg-muted/25 p-4" aria-labelledby={`why-${review.update_id}`}>
            <div className="flex items-center gap-2">
              <FileText className="size-4 text-muted-foreground" aria-hidden="true" />
              <h3 id={`why-${review.update_id}`} className="text-xs font-bold uppercase tracking-[0.12em] text-foreground/70">
                Why this update
              </h3>
            </div>
            <ul className="mt-2 space-y-1.5 text-sm leading-6 text-foreground/85">
              {reasons.map((reason) => <li key={reason}>{reason}</li>)}
            </ul>
          </section>
        )}

        {sourceSummary && (
          <section className="flex items-start gap-3 rounded-xl border border-primary/20 bg-primary/5 p-4" aria-labelledby={`source-${review.update_id}`}>
            <div className="mt-0.5 flex size-8 shrink-0 items-center justify-center rounded-lg bg-background text-primary ring-1 ring-primary/20">
              <Mail className="size-4" aria-hidden="true" />
            </div>
            <div className="min-w-0">
              <h3 id={`source-${review.update_id}`} className="text-xs font-bold uppercase tracking-[0.12em] text-primary">
                Source
              </h3>
              <p className="mt-1 text-sm font-medium text-foreground">{sourceSummary.title}</p>
              {sourceSummary.description && (
                <p className="mt-0.5 break-words text-xs leading-5 text-muted-foreground">
                  {sourceSummary.description}
                </p>
              )}
            </div>
          </section>
        )}

        {protectedChange && (
          <div className="flex items-start gap-3 rounded-xl border border-warning/35 bg-warning/10 p-4 text-sm leading-6">
            <ShieldAlert className="mt-0.5 size-4 shrink-0 text-warning" aria-hidden="true" />
            <p>
              This request changes protected project state. Verify the evidence and transition before approving.
            </p>
          </div>
        )}

        <ReviewDetails
          review={review}
          assessment={assessment}
          citations={citations}
          criteria={criteria}
          policyVersion={policyVersion}
          confidence={confPct}
          decision={decision}
        />
      </CardContent>

      <div className="border-t border-border/60 bg-muted/20 px-5 py-4 sm:px-6 xl:pr-28">
        {rejecting ? (
          <div className="space-y-3">
            <div>
              <p className="text-sm font-semibold text-foreground">Why should this update be rejected?</p>
              <p className="mt-0.5 text-xs text-muted-foreground">
                Select a reason so the agent can improve the next proposal.
              </p>
            </div>
            <div className="flex flex-wrap gap-2">
              {REJECT_TAGS.map((tag) => (
                <button
                  key={tag}
                  type="button"
                  onClick={() => toggleTag(tag)}
                  aria-pressed={selectedTags.includes(tag)}
                  className={cn(
                    "rounded-full border px-3 py-1.5 text-xs font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                    selectedTags.includes(tag)
                      ? "border-primary bg-primary/10 text-primary"
                      : "border-border bg-background text-muted-foreground hover:border-primary/40 hover:text-foreground",
                  )}
                >
                  {tag}
                </button>
              ))}
            </div>
            <textarea
              value={rejectNote}
              onChange={(event) => setRejectNote(event.target.value)}
              placeholder="Optional: add more detail for the agent..."
              className="min-h-20 w-full resize-y rounded-lg border border-border bg-background px-3 py-2.5 text-sm text-foreground placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            />
            <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
              <Button variant="outline" onClick={cancelReject} disabled={submitting !== null}>
                Cancel
              </Button>
              <Button
                variant="destructive"
                onClick={() => void runAction("reject")}
                disabled={submitting !== null}
              >
                <XCircle className="size-4" />
                {submitting === "reject" ? "Submitting..." : "Submit rejection"}
              </Button>
            </div>
          </div>
        ) : (
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <p className="text-xs leading-5 text-muted-foreground">
              Approving applies {changes.length === 1 ? "this change" : `${changes.length} changes`} immediately.
            </p>
            <div className="grid grid-cols-2 gap-2 sm:flex sm:shrink-0">
              <Button
                variant="outline"
                className="border-danger/25 text-danger hover:bg-danger/10 hover:text-danger"
                onClick={() => setRejecting(true)}
                disabled={submitting !== null}
              >
                <XCircle className="size-4" />
                Reject
              </Button>
              <Button
                className="min-w-28"
                onClick={() => void runAction("approve")}
                disabled={submitting !== null || changes.length === 0}
              >
                <CheckCircle2 className="size-4" />
                {submitting === "approve" ? "Approving..." : "Approve"}
              </Button>
            </div>
          </div>
        )}
      </div>
    </Card>
  );
}

function ChangeComparison({ change }: { change: ReviewChange }) {
  if (change.field === "notes") return <NotesChangeComparison change={change} />;
  return (
    <article className="overflow-hidden rounded-xl border border-border/80 bg-background">
      <div className="border-b border-border/60 bg-muted/30 px-4 py-2.5">
        <p className="text-sm font-semibold text-foreground">{formatFieldLabel(change.field)}</p>
      </div>
      <div className="grid items-stretch gap-0 sm:grid-cols-[minmax(0,1fr)_3rem_minmax(0,1.12fr)]">
        <div className="p-4">
          <p className="text-xs font-bold uppercase tracking-[0.12em] text-muted-foreground">Current</p>
          <div className="mt-2 text-sm leading-6 text-foreground/70">
            <ReviewValue value={change.old_value} emptyLabel="Not set" />
          </div>
        </div>

        <div className="flex items-center justify-center border-y border-border/60 bg-muted/20 py-2 sm:border-x sm:border-y-0 sm:py-0">
          <ArrowRight className="size-4 rotate-90 text-muted-foreground sm:rotate-0" aria-hidden="true" />
          <span className="sr-only">changes to</span>
        </div>

        <div className="bg-primary/5 p-4 ring-1 ring-inset ring-primary/15">
          <div className="flex items-center gap-1.5 text-xs font-bold uppercase tracking-[0.12em] text-primary">
            <CheckCircle2 className="size-3.5" aria-hidden="true" />
            Proposed
          </div>
          <div className="mt-2 text-sm font-medium leading-6 text-foreground">
            <ReviewValue value={change.new_value} emptyLabel="Empty" />
          </div>
        </div>
      </div>
    </article>
  );
}

function ReviewValue({ value, emptyLabel }: { value?: string; emptyLabel: string }) {
  if (!value) return <p className="italic text-muted-foreground">{emptyLabel}</p>;
  const parts = splitReviewValue(value);
  if (parts.length === 1) return <p className="break-words">{parts[0]}</p>;
  return (
    <ul className="space-y-1.5">
      {parts.map((part, index) => (
        <li key={`${part}-${index}`} className="flex gap-2">
          <span className="mt-[0.65rem] size-1 shrink-0 rounded-full bg-current opacity-50" aria-hidden="true" />
          <span className="break-words">{part}</span>
        </li>
      ))}
    </ul>
  );
}

function ReviewDetails({
  review,
  assessment,
  citations,
  criteria,
  policyVersion,
  confidence,
  decision,
}: {
  review: ReviewItem;
  assessment: ReviewAssessment | null;
  citations: ReviewCitation[];
  criteria: NonNullable<ReviewAssessment["milestone_criteria"]>;
  policyVersion?: string;
  confidence: number | null;
  decision: string | null;
}) {
  const dimensions = Object.entries(assessment?.confidence ?? {}).filter(
    ([, value]) => typeof value === "number",
  );
  const hasAssessmentDetails = Boolean(
    criteria.length
    || assessment?.uncertainties?.length
    || assessment?.contradictions?.length
    || dimensions.length,
  );
  const detailCount = citations.length;

  return (
    <details className="group rounded-xl border border-border/70 bg-background">
      <summary className="flex min-h-11 cursor-pointer list-none items-center justify-between gap-3 px-4 py-3 text-sm font-medium text-foreground marker:hidden focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring">
        <span className="flex min-w-0 items-center gap-2">
          <FileText className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
          <span>Evidence and review details</span>
          {detailCount > 0 && (
            <span className="rounded-full bg-muted px-2 py-0.5 text-xs font-semibold text-muted-foreground">
              {detailCount} ref{detailCount === 1 ? "" : "s"}
            </span>
          )}
        </span>
        <ChevronDown className="size-4 shrink-0 text-muted-foreground transition-transform group-open:rotate-180" aria-hidden="true" />
      </summary>

      <div className="space-y-4 border-t border-border/60 px-4 py-4">
        <dl className="grid gap-3 text-xs sm:grid-cols-2 lg:grid-cols-4">
          {review.task_type && <DetailTerm label="Task type" value={formatFieldLabel(review.task_type)} />}
          {confidence !== null && <DetailTerm label="Confidence" value={`${confidence}%`} />}
          {decision && <DetailTerm label="Decision" value={formatFieldLabel(decision)} />}
          {policyVersion && <DetailTerm label="Policy" value={policyVersion} />}
          {review.source_type && <DetailTerm label="Source type" value={formatFieldLabel(review.source_type)} />}
          {review.created_at && <DetailTerm label="Created" value={formatReviewDate(review.created_at)} />}
        </dl>

        <EvidenceList citations={citations} />

        {criteria.length > 0 && (
          <div>
            <p className="text-xs font-bold uppercase tracking-[0.12em] text-foreground/70">Milestone criteria</p>
            <div className="mt-2 grid gap-2 md:grid-cols-3">
              {(["met", "missing", "contradicted"] as const).map((result) => {
                const items = criteria.filter((item) => item.result === result);
                return (
                  <div key={result} className="rounded-lg border border-border/60 bg-muted/20 p-3">
                    <p className="text-xs font-semibold capitalize">{result} ({items.length})</p>
                    {items.length > 0 ? items.map((item, index) => (
                      <p key={`${item.criterion_id}-${index}`} className="mt-1.5 text-xs leading-5 text-muted-foreground">
                        {item.criterion_id ?? "Criterion"}: {item.explanation ?? "No explanation supplied."}
                      </p>
                    )) : <p className="mt-1.5 text-xs text-muted-foreground">None</p>}
                  </div>
                );
              })}
            </div>
          </div>
        )}

        {(assessment?.uncertainties?.length || assessment?.contradictions?.length) ? (
          <div className="rounded-lg border border-warning/30 bg-warning/10 p-3 text-xs leading-5 text-foreground">
            {assessment.uncertainties?.map((item, index) => <p key={`u-${index}`}>Uncertainty: {item}</p>)}
            {assessment.contradictions?.map((item, index) => (
              <p key={`c-${index}`}>Contradiction: {item.quote ?? item.source_ref ?? "Evidence requires review."}</p>
            ))}
          </div>
        ) : null}

        {dimensions.length > 0 && (
          <div className="flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted-foreground">
            <span className="font-semibold text-foreground">Confidence dimensions:</span>
            {dimensions.map(([name, value]) => (
              <span key={name}>{formatFieldLabel(name)}: {Math.round(value * 100)}%</span>
            ))}
          </div>
        )}

        {!hasAssessmentDetails && citations.length === 0 && (
          <p className="text-xs text-muted-foreground">No additional assessment details were supplied.</p>
        )}

        <div className="flex justify-end">
          <Link
            href={`/projects/${review.project_id}`}
            className="inline-flex min-h-9 items-center gap-1.5 text-xs font-semibold text-primary underline-offset-4 hover:underline"
          >
            View project
            <ExternalLink className="size-3.5" aria-hidden="true" />
          </Link>
        </div>
      </div>
    </details>
  );
}

function DetailTerm({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg bg-muted/30 p-2.5">
      <dt className="font-semibold uppercase tracking-wide text-muted-foreground">{label}</dt>
      <dd className="mt-1 break-words font-medium text-foreground">{value}</dd>
    </div>
  );
}

function EvidenceList({ citations }: { citations: ReviewCitation[] }) {
  if (citations.length === 0) return null;
  return (
    <div>
      <p className="text-xs font-bold uppercase tracking-[0.12em] text-foreground/70">Evidence references</p>
      <div className="mt-2 space-y-2">
        {citations.slice(0, 6).map((citation, index) => (
          <div key={`${citation.source_ref}-${index}`} className="rounded-lg border border-border/60 bg-muted/20 p-3">
            {citation.quote && <p className="text-sm leading-6 text-foreground">{citation.quote}</p>}
            {citation.source_ref && (
              <div className={cn("text-xs", citation.quote && "mt-2")}>
                <p className="font-semibold text-muted-foreground">{sourceReferenceLabel(citation.source_ref)}</p>
                {isSafeReviewUrl(citation.source_ref) ? (
                  <a
                    href={citation.source_ref}
                    target="_blank"
                    rel="noreferrer"
                    className="mt-1 inline-flex max-w-full items-center gap-1 break-all font-mono text-primary hover:underline"
                  >
                    {citation.source_ref}
                    <ExternalLink className="size-3 shrink-0" aria-hidden="true" />
                  </a>
                ) : (
                  <p className="mt-1 break-all font-mono text-xs leading-5 text-muted-foreground">
                    {citation.source_ref}
                  </p>
                )}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

function formatFieldLabel(value: string): string {
  const normalized = value.trim().toLowerCase();
  if (FIELD_LABELS[normalized]) return FIELD_LABELS[normalized];
  const words = normalized.replaceAll("-", "_").split("_").filter(Boolean).join(" ");
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : value;
}

function formatReviewDate(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function splitReviewValue(value: string): string[] {
  const parts = value.split(/;\s+/).map((item) => item.trim()).filter(Boolean);
  return parts.length > 0 ? parts : [value];
}

function uniqueReasons(changes: ReviewChange[]): string[] {
  return changes
    .map((change) => change.reason)
    .filter((reason): reason is string => Boolean(reason))
    .filter((reason, index, all) => all.indexOf(reason) === index)
    .slice(0, 3);
}

function isSourceOnlyReason(reason: string): boolean {
  return /^Inbound email from\s+[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}$/i.test(reason.trim());
}

function buildSourceSummary(
  review: ReviewItem,
  reasons: string[],
  citations: ReviewCitation[],
): { title: string; description?: string } | null {
  const email = reasons.join(" ").match(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/i)?.[0];
  if (email) {
    return {
      title: "Inbound email",
      description: `From ${email}${review.client_name ? ` for ${review.client_name}` : ""}`,
    };
  }
  if (review.source_type) {
    return {
      title: formatFieldLabel(review.source_type),
      description: review.client_name ?? undefined,
    };
  }
  if (citations.length > 0) {
    return {
      title: `${citations.length} evidence reference${citations.length === 1 ? "" : "s"}`,
      description: "Open the review details to inspect the source identifiers.",
    };
  }
  return null;
}

function sourceReferenceLabel(value: string): string {
  if (isSafeReviewUrl(value)) return "Source link";
  if (/^[0-9a-f]{8}-[0-9a-f-]{27,}$/i.test(value)) return "Evidence record";
  if (/^<[^>]+@[^>]+>$/.test(value)) return "Email message";
  if (value.includes("@")) return "Email source";
  return "Source reference";
}

function assessmentDecision(assessment: ReviewAssessment | null): string | null {
  return assessment?.milestone_assessment?.decision ?? assessment?.status_assessment?.decision ?? null;
}

function riskBorder(risk: string | null, decision: string | null): string {
  if (decision === "contradiction" || decision === "clarification_required" || risk?.toLowerCase() === "high") {
    return "border-l-danger";
  }
  if (decision === "needs_review" || risk?.toLowerCase() === "medium") return "border-l-warning";
  return "border-l-primary";
}

function riskTone(risk: string): string {
  if (risk.toLowerCase() === "high") return "border-danger/30 bg-danger/10 text-danger";
  if (risk.toLowerCase() === "medium") return "border-warning/30 bg-warning/10 text-warning";
  return "border-success/30 bg-success/10 text-success";
}

function ReviewsSkeleton() {
  return (
    <div className="space-y-5">
      {Array.from({ length: 2 }).map((_, index) => (
        <Card key={index} className="overflow-hidden border border-border/80 border-l-4 border-l-border/40 bg-card shadow-sm">
          <CardHeader className="border-b border-border/60 p-5 sm:p-6">
            <div className="flex items-start justify-between gap-4">
              <div className="space-y-3">
                <Skeleton className="h-5 w-28 rounded-full" />
                <Skeleton className="h-6 w-44" />
                <Skeleton className="h-4 w-56" />
              </div>
              <Skeleton className="h-4 w-24" />
            </div>
          </CardHeader>
          <CardContent className="space-y-4 p-5 sm:p-6">
            <Skeleton className="h-4 w-36" />
            <Skeleton className="h-48 w-full rounded-xl" />
            <Skeleton className="h-20 w-full rounded-xl" />
          </CardContent>
          <div className="border-t border-border/60 px-5 py-4 sm:px-6">
            <div className="flex justify-end gap-2">
              <Skeleton className="h-10 w-24 rounded-md" />
              <Skeleton className="h-10 w-28 rounded-md" />
            </div>
          </div>
        </Card>
      ))}
    </div>
  );
}
