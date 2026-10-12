"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import {
  AlertTriangle,
  Bot,
  Eye,
  FileKey2,
  RefreshCw,
  Search,
  ShieldCheck,
  ShieldOff,
  Stethoscope,
} from "lucide-react";

import { PageHeader } from "@/components/page-header";
import { PageToolbar } from "@/components/page-toolbar";
import { ErrorPanel } from "@/components/state-panel";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  ApiError,
  diagnoseTraceRun,
  getTraceRunDetail,
  getTraceRuns,
  revealTraceEvent,
} from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import type { TraceRun, TraceRunDetail } from "@/lib/types";
import { cn } from "@/lib/utils";

function formatDate(value: string | null): string {
  if (!value) return "—";
  const date = new Date(value.endsWith("Z") ? value : `${value}Z`);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function formatDuration(value: number | null): string {
  if (value === null) return "—";
  return value >= 1_000 ? `${(value / 1_000).toFixed(2)} s` : `${value} ms`;
}

export default function TracesPage() {
  const { hasPermission } = useAuth();
  const canDiagnose = hasPermission("traces.diagnose");
  const canReveal = hasPermission("traces.reveal");
  const [runs, setRuns] = useState<TraceRun[]>([]);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [selected, setSelected] = useState<TraceRunDetail | null>(null);
  const [errorsOnly, setErrorsOnly] = useState(false);
  const [taskInput, setTaskInput] = useState("");
  const [taskFilter, setTaskFilter] = useState("");
  const [loadingRuns, setLoadingRuns] = useState(true);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [revealed, setRevealed] = useState<Record<string, unknown>>({});

  const loadRuns = useCallback(async () => {
    setLoadingRuns(true);
    setError("");
    try {
      const params: Record<string, string> = { limit: "100" };
      if (errorsOnly) params.errors_only = "true";
      if (taskFilter) params.task = taskFilter;
      const data = await getTraceRuns(params);
      setRuns(data);
      setSelectedRunId((current) => {
        if (current && data.some((run) => run.run_id === current)) return current;
        return data[0]?.run_id ?? null;
      });
      if (data.length === 0) setSelected(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load trace runs.");
    } finally {
      setLoadingRuns(false);
    }
  }, [errorsOnly, taskFilter]);

  const loadDetail = useCallback(async (runId: string) => {
    setLoadingDetail(true);
    setError("");
    try {
      setSelected(await getTraceRunDetail(runId));
      setRevealed({});
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load the trace timeline.");
    } finally {
      setLoadingDetail(false);
    }
  }, []);

  useEffect(() => { void loadRuns(); }, [loadRuns]);
  useEffect(() => {
    if (selectedRunId) void loadDetail(selectedRunId);
  }, [loadDetail, selectedRunId]);

  function applyTaskFilter(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setTaskFilter(taskInput.trim());
  }

  async function refresh() {
    await loadRuns();
    if (selectedRunId) await loadDetail(selectedRunId);
  }

  async function diagnose() {
    if (!selected || !canDiagnose) return;
    setBusyAction("diagnose");
    setError("");
    try {
      await diagnoseTraceRun(selected.run_id);
      await loadDetail(selected.run_id);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not diagnose this trace run.");
    } finally {
      setBusyAction(null);
    }
  }

  async function reveal(eventId: string) {
    if (!canReveal) return;
    setBusyAction(eventId);
    setError("");
    try {
      const result = await revealTraceEvent(eventId);
      setRevealed((current) => ({ ...current, [eventId]: result.payload }));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not reveal the encrypted payload.");
    } finally {
      setBusyAction(null);
    }
  }

  return (
    <div className="mx-auto max-w-[1700px] space-y-6">
      <PageHeader
        eyebrow="AI & Analytics"
        title="Trace explorer"
        description="Review execution timing, tool names, outcomes, and diagnostics. Sensitive payload reveal is Admin-only and every access is audited."
        metadata={<>{runs.length} trace run{runs.length === 1 ? "" : "s"} <span aria-hidden="true">•</span> Sanitized read model</>}
        actions={
          <Button variant="outline" onClick={() => void refresh()} disabled={loadingRuns || loadingDetail || busyAction !== null}>
            <RefreshCw className={cn("size-4", (loadingRuns || loadingDetail) && "animate-spin")} />
            Refresh
          </Button>
        }
      />

      <Card className="border-primary/20 bg-primary/5">
        <CardContent className="flex gap-3 py-1">
          <ShieldCheck className="mt-0.5 size-5 shrink-0 text-primary" />
          <p className="text-sm leading-6 text-muted-foreground">
            This page uses a fixed sanitized schema. Raw prompts, responses, tool arguments, chat identifiers, and operator identifiers are not included in standard trace responses.
          </p>
        </CardContent>
      </Card>

      {error ? <ErrorPanel description={error} onRetry={() => void refresh()} /> : null}

      <PageToolbar summary={`${runs.length} runs match the current filters`}>
        <form className="flex min-w-0 flex-1 flex-col gap-2 sm:flex-row" onSubmit={applyTaskFilter}>
          <label className="relative min-w-0 flex-1">
            <Search className="absolute left-3 top-3.5 size-4 text-muted-foreground" />
            <span className="sr-only">Filter traces by exact task name</span>
            <Input
              className="pl-10"
              value={taskInput}
              onChange={(event) => setTaskInput(event.target.value)}
              placeholder="Exact task name"
            />
          </label>
          <Button type="submit" variant="outline">Apply filter</Button>
          <Button
            type="button"
            variant={errorsOnly ? "default" : "outline"}
            aria-pressed={errorsOnly}
            onClick={() => setErrorsOnly((value) => !value)}
          >
            <AlertTriangle className="size-4" />
            Errors only
          </Button>
        </form>
      </PageToolbar>

      <div className="grid gap-4 xl:grid-cols-[22rem_minmax(0,1fr)]">
        <Card className="overflow-hidden">
          <CardHeader className="border-b py-4">
            <CardTitle className="text-sm">Runs ({runs.length})</CardTitle>
          </CardHeader>
          <CardContent className="max-h-[72vh] space-y-2 overflow-y-auto p-3">
            {loadingRuns && runs.length === 0 ? (
              <div className="flex min-h-40 items-center justify-center text-sm text-muted-foreground" role="status">
                <span className="mr-3 size-4 animate-spin rounded-full border-2 border-primary border-t-transparent" />
                Loading trace runs…
              </div>
            ) : runs.length === 0 ? (
              <p className="p-6 text-center text-sm text-muted-foreground">No trace runs match these filters.</p>
            ) : runs.map((run) => (
              <button
                key={run.run_id}
                type="button"
                onClick={() => setSelectedRunId(run.run_id)}
                aria-pressed={selectedRunId === run.run_id}
                className={cn(
                  "min-h-20 w-full rounded-xl border p-3 text-left transition-colors hover:bg-muted/50 focus-visible:outline-none focus-visible:ring-3 focus-visible:ring-ring/40",
                  selectedRunId === run.run_id && "border-primary bg-primary/5",
                )}
              >
                <div className="flex items-start justify-between gap-2">
                  <span className="line-clamp-2 text-sm font-medium leading-5">{run.task}</span>
                  <StatusPill value={run.status} />
                </div>
                <p className="mt-2 text-xs text-muted-foreground">{run.surface} · {formatDate(run.started_at)}</p>
                <p className="mt-1 truncate font-mono text-xs text-muted-foreground">{run.run_id}</p>
              </button>
            ))}
          </CardContent>
        </Card>

        <div className="min-w-0 space-y-4">
          {loadingDetail && !selected ? (
            <Card><CardContent className="flex min-h-56 items-center justify-center text-sm text-muted-foreground">Loading trace timeline…</CardContent></Card>
          ) : selected ? (
            <>
              <Card>
                <CardHeader>
                  <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                    <div>
                      <CardTitle className="flex items-center gap-2 text-base"><Bot className="size-4" />{selected.task}</CardTitle>
                      <p className="mt-1 break-all font-mono text-xs text-muted-foreground">{selected.run_id}</p>
                    </div>
                    {canDiagnose ? (
                      <Button size="sm" onClick={() => void diagnose()} disabled={busyAction !== null || loadingDetail}>
                        <Stethoscope className="size-4" />
                        Diagnose
                      </Button>
                    ) : null}
                  </div>
                </CardHeader>
                <CardContent className="grid gap-4 text-sm sm:grid-cols-2 lg:grid-cols-4">
                  <Metric label="Status"><StatusPill value={selected.status} /></Metric>
                  <Metric label="Exit reason" value={selected.exit_reason ?? "—"} />
                  <Metric label="Duration" value={formatDuration(selected.duration_ms)} />
                  <Metric label="Project" value={selected.project_id ?? "Shared / none"} />
                  <Metric label="Started" value={formatDate(selected.started_at)} />
                  <Metric label="Ended" value={formatDate(selected.ended_at)} />
                  <Metric label="Surface" value={selected.surface} />
                  <Metric label="Invocation" value={selected.implicit ? "Implicit" : "Explicit"} />
                </CardContent>
              </Card>

              {selected.diagnostics.length > 0 ? (
                <div className="space-y-3">
                  {selected.diagnostics.map((diagnostic) => (
                    <Card key={diagnostic.diagnostic_id} className="border-warning/35">
                      <CardHeader className="pb-2">
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <CardTitle className="text-sm">Diagnosis</CardTitle>
                          <div className="flex gap-2">
                            {diagnostic.severity ? <Badge variant="outline">{diagnostic.severity}</Badge> : null}
                            <StatusPill value={diagnostic.status} />
                          </div>
                        </div>
                      </CardHeader>
                      <CardContent className="space-y-2 text-sm">
                        <p><span className="font-medium">Root cause:</span> {diagnostic.root_cause ?? "Not determined"}</p>
                        <p className="leading-6 text-muted-foreground">{diagnostic.summary ?? "No summary was generated."}</p>
                        <p className="text-xs text-muted-foreground">Trigger: {diagnostic.trigger} · Created {formatDate(diagnostic.created_at)}</p>
                      </CardContent>
                    </Card>
                  ))}
                </div>
              ) : null}

              {selected.llm_traces.length > 0 ? (
                <Card>
                  <CardHeader><CardTitle className="flex items-center gap-2 text-sm"><FileKey2 className="size-4" />LLM summaries</CardTitle></CardHeader>
                  <CardContent className="space-y-3">
                    {selected.llm_traces.map((trace) => (
                      <div key={trace.trace_id} className="grid gap-3 rounded-xl border p-3 text-sm sm:grid-cols-2 lg:grid-cols-4">
                        <Metric label="Task" value={trace.task} />
                        <Metric label="Tokens" value={`${trace.tokens_in} in / ${trace.tokens_out} out`} />
                        <Metric label="Latency" value={formatDuration(trace.latency_ms)} />
                        <Metric label="Output" value={trace.output_valid ? "Valid" : "Invalid"} />
                        <Metric label="Decision" value={trace.decision_outcome ?? "—"} />
                        <Metric label="Error class" value={trace.error_class ?? "none"} />
                        <Metric label="Prompt hash" value={trace.prompt_hash ?? "—"} mono />
                        <Metric label="Created" value={formatDate(trace.created_at)} />
                      </div>
                    ))}
                  </CardContent>
                </Card>
              ) : null}

              <Card>
                <CardHeader><CardTitle className="text-sm">Timeline ({selected.events.length})</CardTitle></CardHeader>
                <CardContent className="space-y-3">
                  {selected.events.length === 0 ? (
                    <p className="py-8 text-center text-sm text-muted-foreground">No timeline events were recorded.</p>
                  ) : selected.events.map((event) => (
                    <div key={event.event_id} className="rounded-xl border p-3">
                      <div className="flex flex-wrap items-start justify-between gap-2">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="font-mono text-xs text-muted-foreground">#{event.sequence}</span>
                          <span className="text-sm font-medium">{event.event_type}</span>
                          {event.round_index !== null ? <Badge variant="outline">Round {event.round_index}</Badge> : null}
                          {event.tool_name ? <Badge variant="secondary">{event.tool_name}</Badge> : null}
                        </div>
                        <StatusPill value={event.status} />
                      </div>
                      <p className="mt-2 text-xs text-muted-foreground">
                        {event.duration_ms !== null ? `${formatDuration(event.duration_ms)} · ` : ""}{formatDate(event.created_at)}
                      </p>
                      {event.llm_trace_id ? <p className="mt-1 break-all font-mono text-xs text-muted-foreground">LLM trace: {event.llm_trace_id}</p> : null}

                      {event.payload_available ? (
                        canReveal ? (
                          revealed[event.event_id] === undefined ? (
                            <Button
                              variant="outline"
                              size="sm"
                              className="mt-3"
                              onClick={() => void reveal(event.event_id)}
                              disabled={busyAction !== null}
                              aria-label={`Reveal encrypted payload for event ${event.sequence}`}
                            >
                              <Eye className="size-4" />
                              {busyAction === event.event_id ? "Revealing…" : "Reveal sensitive payload"}
                            </Button>
                          ) : (
                            <pre className="mt-3 max-h-80 overflow-auto rounded-lg border bg-muted p-3 text-xs">{JSON.stringify(revealed[event.event_id], null, 2)}</pre>
                          )
                        ) : (
                          <div className="mt-3 flex items-center gap-2 rounded-lg border border-dashed px-3 py-2 text-xs text-muted-foreground">
                            <ShieldOff className="size-4" />
                            Sensitive payload hidden. Admin permission is required to reveal it.
                          </div>
                        )
                      ) : null}
                    </div>
                  ))}
                </CardContent>
              </Card>
            </>
          ) : (
            <Card><CardContent className="flex min-h-56 items-center justify-center p-10 text-center text-muted-foreground">Select a run to inspect its sanitized timeline.</CardContent></Card>
          )}
        </div>
      </div>
    </div>
  );
}

function Metric({ label, value, children, mono = false }: { label: string; value?: string; children?: React.ReactNode; mono?: boolean }) {
  return (
    <div className="min-w-0">
      <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">{label}</p>
      {children ?? <p className={cn("mt-1 truncate", mono && "font-mono text-xs")} title={value}>{value}</p>}
    </div>
  );
}

function StatusPill({ value }: { value: string }) {
  return (
    <span className={cn(
      "inline-flex h-6 w-fit items-center rounded-full border px-2 text-xs font-medium",
      value === "error" || value === "failed"
        ? "border-danger/30 bg-danger/10 text-danger"
        : value === "running" || value === "pending"
          ? "border-info/30 bg-info/10 text-info"
          : "border-success/30 bg-success/10 text-success",
    )}>
      {value}
    </span>
  );
}
