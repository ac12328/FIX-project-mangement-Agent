"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Clock3, Globe2, LogOut, MonitorSmartphone, RefreshCw, ShieldCheck } from "lucide-react";

import { PageHeader } from "@/components/page-header";
import { ErrorPanel, LoadingPanel, StatePanel } from "@/components/state-panel";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { type AuthSessionRecord, ApiError, getMySessions, revokeMySession } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { notifyError, notifySuccess } from "@/lib/notifications";

function formatDate(value: string | null): string {
  if (!value) return "Unavailable";
  const date = new Date(value.endsWith("Z") ? value : `${value}Z`);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function deviceLabel(userAgent: string | null): string {
  if (!userAgent) return "Unknown device";
  if (/mobile|android|iphone|ipad/i.test(userAgent)) return "Mobile browser";
  return "Desktop browser";
}

export default function SessionsPage() {
  const router = useRouter();
  const { refreshSession, signOutAll } = useAuth();
  const [sessions, setSessions] = useState<AuthSessionRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setSessions(await getMySessions());
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : "Could not load your sessions.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  async function revoke(record: AuthSessionRecord) {
    setBusyId(record.session_id);
    setError("");
    try {
      await revokeMySession(record.session_id);
      if (record.current) {
        await refreshSession();
        router.replace("/login");
        router.refresh();
        return;
      }
      notifySuccess("Session revoked");
      await load();
    } catch (cause) {
      const message = cause instanceof ApiError ? cause.message : "Could not revoke the session.";
      setError(message);
      notifyError(cause, message);
    } finally {
      setBusyId(null);
    }
  }

  async function revokeAll() {
    if (!window.confirm("Sign out all Dashboard sessions, including this one?")) return;
    setBusyId("all");
    try {
      await signOutAll();
      router.replace("/login");
      router.refresh();
    } catch (cause) {
      notifyError(cause, "Could not sign out all sessions.");
    } finally {
      setBusyId(null);
    }
  }

  return (
    <div className="mx-auto max-w-6xl space-y-6">
      <PageHeader
        eyebrow="Account security"
        title="My sessions"
        description="Review active browsers and revoke access you no longer recognize."
        metadata={`${sessions.length} active session${sessions.length === 1 ? "" : "s"}`}
        actions={
          <>
            <Button variant="outline" onClick={() => void load()} disabled={loading || busyId !== null}>
              <RefreshCw className={loading ? "size-4 animate-spin" : "size-4"} />
              Refresh
            </Button>
            <Button variant="destructive" onClick={() => void revokeAll()} disabled={busyId !== null || sessions.length === 0}>
              <LogOut className="size-4" />
              Sign out all
            </Button>
          </>
        }
      />

      <Card className="border-primary/20 bg-primary/5 shadow-xs">
        <CardContent className="flex gap-3 py-4">
          <ShieldCheck className="mt-0.5 size-5 shrink-0 text-primary" aria-hidden="true" />
          <p className="text-sm leading-6 text-muted-foreground">
            Dashboard sessions expire after two hours of inactivity and no later than twelve hours after sign-in. Role and account-status changes take effect immediately.
          </p>
        </CardContent>
      </Card>

      {error ? <ErrorPanel description={error} onRetry={() => void load()} /> : null}

      {loading ? (
        <LoadingPanel label="Loading active sessions" />
      ) : sessions.length === 0 ? (
        <StatePanel
          icon={MonitorSmartphone}
          title="No active sessions"
          description="No browser sessions are currently associated with this account."
          actionLabel="Refresh"
          onAction={() => void load()}
        />
      ) : (
        <div className="grid gap-4 md:grid-cols-2">
          {sessions.map((record) => (
            <Card key={record.session_id} className={record.current ? "ring-2 ring-primary/30" : undefined}>
              <CardHeader>
                <div className="flex items-start justify-between gap-3">
                  <div className="flex min-w-0 items-center gap-3">
                    <span className="flex size-11 shrink-0 items-center justify-center rounded-xl bg-muted text-muted-foreground">
                      <MonitorSmartphone className="size-5" aria-hidden="true" />
                    </span>
                    <div className="min-w-0">
                      <CardTitle>{deviceLabel(record.user_agent)}</CardTitle>
                      <CardDescription className="truncate">{record.auth_method === "entra" ? "Microsoft Entra ID" : record.auth_method}</CardDescription>
                    </div>
                  </div>
                  <Badge variant={record.current ? "default" : "outline"}>{record.current ? "Current" : "Active"}</Badge>
                </div>
              </CardHeader>
              <CardContent className="space-y-4">
                <dl className="grid gap-3 text-sm sm:grid-cols-2">
                  <SessionValue label="IP address"><span className="inline-flex items-center gap-1.5 font-mono"><Globe2 className="size-3.5" />{record.ip_address || "Unknown"}</span></SessionValue>
                  <SessionValue label="Last seen">{formatDate(record.last_seen_at)}</SessionValue>
                  <SessionValue label="Created">{formatDate(record.created_at)}</SessionValue>
                  <SessionValue label="Idle expiry"><span className="inline-flex items-center gap-1.5"><Clock3 className="size-3.5" />{formatDate(record.idle_expires_at)}</span></SessionValue>
                </dl>
                <details className="rounded-xl border bg-muted/20 px-3 py-2 text-xs text-muted-foreground">
                  <summary className="flex min-h-11 cursor-pointer items-center font-medium text-foreground">Session details</summary>
                  <p className="mt-2 break-all font-mono">{record.session_id}</p>
                  <p className="mt-2 break-words">{record.user_agent || "User agent unavailable"}</p>
                  <p className="mt-2">Absolute expiry: {formatDate(record.absolute_expires_at)}</p>
                </details>
                <Button
                  variant={record.current ? "destructive" : "outline"}
                  className="w-full"
                  disabled={busyId !== null}
                  onClick={() => void revoke(record)}
                >
                  <LogOut className="size-4" />
                  {busyId === record.session_id ? "Revoking..." : record.current ? "Sign out this session" : "Revoke session"}
                </Button>
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}

function SessionValue({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <dt className="text-xs font-medium text-muted-foreground">{label}</dt>
      <dd className="mt-1 text-xs text-foreground">{children}</dd>
    </div>
  );
}
