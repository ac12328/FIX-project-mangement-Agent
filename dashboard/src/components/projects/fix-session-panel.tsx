"use client";

import { useCallback, useEffect, useState } from "react";
import { AlertTriangle, CheckCircle2, ChevronDown, ChevronUp, History, Server } from "lucide-react";
import { PermissionGuard } from "@/components/permission-guard";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  approveFixSessionCandidate,
  getFixSessionHistory,
  getPendingFixSessionCandidates,
  getProjectFixSessions,
  rejectFixSessionCandidate,
} from "@/lib/api";
import type {
  FixSessionCandidate,
  FixSessionHistoryRecord,
  FixSessionRecord,
} from "@/lib/types";

function value(value: string | number | null | undefined): string {
  return value === null || value === undefined || value === "" ? "Not recorded" : String(value);
}

function formatDate(raw: string | null | undefined): string {
  return raw ? new Date(raw).toLocaleString() : "Unknown";
}

function EnvironmentBadge({ environment }: { environment: string }) {
  const production = environment.toUpperCase() === "PRODUCTION";
  return (
    <Badge variant={production ? "default" : "secondary"} className="text-xs">
      {environment.toUpperCase()}
    </Badge>
  );
}

export default function FixSessionPanel({ projectId }: { projectId: string }) {
  const [sessions, setSessions] = useState<FixSessionRecord[]>([]);
  const [candidates, setCandidates] = useState<FixSessionCandidate[]>([]);
  const [history, setHistory] = useState<Record<string, FixSessionHistoryRecord[]>>({});
  const [openHistory, setOpenHistory] = useState<Record<string, boolean>>({});
  const [loading, setLoading] = useState(true);
  const [busyCandidate, setBusyCandidate] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [sessionRows, candidateRows] = await Promise.all([
        getProjectFixSessions(projectId),
        getPendingFixSessionCandidates(projectId),
      ]);
      setSessions(sessionRows);
      setCandidates(candidateRows);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load FIX session configuration.");
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => { void refresh(); }, [refresh]);

  const toggleHistory = async (session: FixSessionRecord) => {
    const key = `${session.environment}:${session.session_key}`;
    const nextOpen = !openHistory[key];
    setOpenHistory((current) => ({ ...current, [key]: nextOpen }));
    if (nextOpen && !history[key]) {
      try {
        const rows = await getFixSessionHistory(projectId, session.environment, session.session_key);
        setHistory((current) => ({ ...current, [key]: rows }));
      } catch (err) {
        setError(err instanceof Error ? err.message : "Could not load session history.");
      }
    }
  };

  const decide = async (candidate: FixSessionCandidate, decision: "approve" | "reject") => {
    const reason = window.prompt(
      decision === "approve"
        ? "Approval reason (required):"
        : "Rejection reason (required):",
    );
    if (!reason?.trim()) return;
    setBusyCandidate(candidate.candidate_id);
    setError(null);
    try {
      if (decision === "approve") {
        await approveFixSessionCandidate(candidate.candidate_id, reason.trim());
      } else {
        await rejectFixSessionCandidate(candidate.candidate_id, reason.trim());
      }
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : `Could not ${decision} candidate.`);
    } finally {
      setBusyCandidate(null);
    }
  };

  if (loading) return <p className="text-sm text-muted-foreground">Loading canonical session records...</p>;

  return (
    <div className="space-y-4">
      <div className="rounded-md border border-blue-500/20 bg-blue-500/5 p-3 text-xs text-muted-foreground">
        This view shows verified, versioned project records only. UAT and Production are isolated. Credentials are never displayed in the dashboard.
      </div>

      {error && <div className="rounded-md border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive">{error}</div>}

      {candidates.length > 0 && (
        <Card className="border-amber-500/30">
          <CardHeader className="pb-2">
            <CardTitle className="flex items-center gap-2 text-sm">
              <AlertTriangle className="h-4 w-4 text-amber-500" /> Pending configuration review
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {candidates.map((candidate) => (
              <div key={candidate.candidate_id} className="rounded-md border p-3 text-sm">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <EnvironmentBadge environment={candidate.environment} />
                    <span className="font-medium">{candidate.session_key}</span>
                  </div>
                  <span className="text-xs text-muted-foreground">Received {formatDate(candidate.received_at || candidate.created_at)}</span>
                </div>
                <div className="mt-2 grid gap-2 text-xs sm:grid-cols-2">
                  <div><span className="text-muted-foreground">Sender:</span> {value(candidate.sender_email)}</div>
                  <div><span className="text-muted-foreground">Confidence / completeness:</span> {Math.round(candidate.confidence * 100)}% / {Math.round(candidate.completeness * 100)}%</div>
                </div>
                <div className="mt-2 text-xs">
                  <span className="font-medium">Changed:</span> {candidate.changed_fields.join(", ") || "None"}
                </div>
                {candidate.inherited_fields.length > 0 && (
                  <div className="mt-1 text-xs text-muted-foreground">Inherited from current verified version: {candidate.inherited_fields.join(", ")}</div>
                )}
                {candidate.conflict_reasons.length > 0 && (
                  <div className="mt-2 rounded bg-amber-500/10 p-2 text-xs text-amber-700 dark:text-amber-300">
                    {candidate.conflict_reasons.join("; ")}
                  </div>
                )}
                <PermissionGuard permission="business.write">
                  <div className="mt-3 flex gap-2">
                    <Button size="sm" onClick={() => void decide(candidate, "approve")} disabled={busyCandidate === candidate.candidate_id}>Approve</Button>
                    <Button size="sm" variant="outline" onClick={() => void decide(candidate, "reject")} disabled={busyCandidate === candidate.candidate_id}>Reject</Button>
                  </div>
                </PermissionGuard>
              </div>
            ))}
          </CardContent>
        </Card>
      )}

      {sessions.length === 0 ? (
        <Card><CardContent className="py-8 text-center text-sm text-muted-foreground">No verified FIX session record exists for this project.</CardContent></Card>
      ) : (
        sessions.map((session) => {
          const key = `${session.environment}:${session.session_key}`;
          return (
            <Card key={session.session_version_id}>
              <CardHeader className="pb-2">
                <CardTitle className="flex flex-wrap items-center justify-between gap-2 text-sm">
                  <span className="flex items-center gap-2"><Server className="h-4 w-4" /><EnvironmentBadge environment={session.environment} /> {session.session_key}</span>
                  <span className="flex items-center gap-2 text-xs font-normal text-muted-foreground"><CheckCircle2 className="h-3.5 w-3.5 text-emerald-500" /> Verified version {session.version_no}</span>
                </CardTitle>
              </CardHeader>
              <CardContent className="space-y-3">
                <div className="grid gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
                  <div><span className="text-muted-foreground">BeginString:</span> {value(session.begin_string)}</div>
                  <div><span className="text-muted-foreground">Heartbeat:</span> {session.heartbeat_seconds == null ? "Not recorded" : `${session.heartbeat_seconds}s`}</div>
                  <div><span className="text-muted-foreground">SenderCompID:</span> {value(session.sender_comp_id)}</div>
                  <div><span className="text-muted-foreground">TargetCompID:</span> {value(session.target_comp_id)}</div>
                  <div><span className="text-muted-foreground">Account reference:</span> {value(session.account_reference)}</div>
                  <div><span className="text-muted-foreground">Reset on logon:</span> {session.reset_seq_num_on_logon == null ? "Not recorded" : session.reset_seq_num_on_logon ? "Yes" : "No"}</div>
                </div>

                <div>
                  <p className="mb-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground">Endpoints</p>
                  {session.endpoints.length === 0 ? <p className="text-sm text-muted-foreground">No endpoint recorded.</p> : session.endpoints.map((endpoint, index) => (
                    <div key={`${endpoint.route_role}-${endpoint.host}-${endpoint.port}-${index}`} className="flex flex-wrap justify-between gap-2 border-t py-1.5 text-sm first:border-t-0">
                      <span>{endpoint.route_role} · {endpoint.transport}</span><code className="text-xs">{endpoint.host}:{endpoint.port}</code>
                    </div>
                  ))}
                </div>

                <div className="flex flex-wrap gap-2 text-xs">
                  <Badge variant="outline">Username secret: {session.has_username_secret ? "stored" : "not stored"}</Badge>
                  <Badge variant="outline">Password secret: {session.has_password_secret ? "stored" : "not stored"}</Badge>
                </div>

                <div className="flex flex-wrap items-center justify-between gap-2 border-t pt-3 text-xs text-muted-foreground">
                  <span>Confirmed {formatDate(session.confirmed_at)} by {value(session.confirmed_by)} ({session.confirmation_mode})</span>
                  <Button size="sm" variant="ghost" onClick={() => void toggleHistory(session)}>
                    <History className="mr-1 h-3.5 w-3.5" /> History {openHistory[key] ? <ChevronUp className="ml-1 h-3.5 w-3.5" /> : <ChevronDown className="ml-1 h-3.5 w-3.5" />}
                  </Button>
                </div>

                {openHistory[key] && (
                  <div className="rounded-md bg-muted/40 p-3">
                    {!history[key] ? <p className="text-xs text-muted-foreground">Loading history...</p> : history[key].map((row) => (
                      <div key={row.session_version_id} className="flex flex-wrap justify-between gap-2 border-b py-2 text-xs last:border-b-0">
                        <span>Version {row.version_no} · {row.is_current ? "current" : "superseded"}</span>
                        <span>{formatDate(row.confirmed_at)} · {value(row.confirmed_by)} · {row.confirmation_mode}</span>
                      </div>
                    ))}
                  </div>
                )}
              </CardContent>
            </Card>
          );
        })
      )}
    </div>
  );
}
