"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import {
  ChevronDown,
  ChevronUp,
  KeyRound,
  MailCheck,
  MailWarning,
  BellRing,
  MailQuestionMark,
  RefreshCw,
  Search,
  Send,
  Smartphone,
  UserRoundCog,
} from "lucide-react";

import { UserSessions } from "@/components/admin/user-sessions";
import { ReminderDeliveries } from "@/components/admin/reminder-deliveries";
import { DraftOutboxReconciliation } from "@/components/admin/draft-outbox-reconciliation";
import { PageHeader } from "@/components/page-header";
import { ErrorState } from "@/components/error-state";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  ApiError,
  type AdminUser,
  type AuthSessionRecord,
  type DashboardRole,
  type DashboardUserStatus,
  type SecurityEvent,
  getAdminUserSessions,
  getAdminUsers,
  getSecurityEvents,
  revokeAdminSession,
  revokeAllAdminUserSessions,
  bindAdminTelegramIdentity,
  sendAdminPasswordReset,
  unbindAdminTelegramIdentity,
  updateAdminUser,
} from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { cn } from "@/lib/utils";

type Notice = { tone: "success" | "error"; message: string } | null;

function formatDate(value: string | null): string {
  if (!value) return "Never";
  const date = new Date(value.endsWith("Z") ? value : `${value}Z`);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function errorMessage(error: unknown, fallback: string): string {
  if (error instanceof ApiError) {
    return error.status === 409 ? `Change blocked: ${error.message}` : error.message;
  }
  return error instanceof Error ? error.message : fallback;
}

function severityClass(severity: string): string {
  if (severity === "critical" || severity === "error") return "border-danger/30 bg-danger/10 text-danger";
  if (severity === "warning") return "border-warning/30 bg-warning/10 text-warning";
  return "border-info/30 bg-info/10 text-info";
}

export default function AdminPage() {
  const { session, refreshSession, hasPermission } = useAuth();
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [sessionsByUser, setSessionsByUser] = useState<Record<string, AuthSessionRecord[]>>({});
  const [expandedUserId, setExpandedUserId] = useState<string | null>(null);
  const [securityEvents, setSecurityEvents] = useState<SecurityEvent[]>([]);
  const [searchInput, setSearchInput] = useState("");
  const [userSearch, setUserSearch] = useState("");
  const [eventTypeInput, setEventTypeInput] = useState("");
  const [eventType, setEventType] = useState("");
  const [loadingUsers, setLoadingUsers] = useState(true);
  const [loadingEvents, setLoadingEvents] = useState(true);
  const [loadingSessions, setLoadingSessions] = useState<string | null>(null);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [telegramDrafts, setTelegramDrafts] = useState<Record<string, string>>({});
  const [usersError, setUsersError] = useState<string | null>(null);
  const [eventsError, setEventsError] = useState<string | null>(null);
  const [notice, setNotice] = useState<Notice>(null);

  const loadUsers = useCallback(async () => {
    setLoadingUsers(true);
    setUsersError(null);
    try {
      setUsers(await getAdminUsers(userSearch));
    } catch (error) {
      setUsersError(errorMessage(error, "Could not load Dashboard users."));
    } finally {
      setLoadingUsers(false);
    }
  }, [userSearch]);

  const loadEvents = useCallback(async () => {
    setLoadingEvents(true);
    setEventsError(null);
    try {
      setSecurityEvents(await getSecurityEvents({ eventType, limit: 200 }));
    } catch (error) {
      setEventsError(errorMessage(error, "Could not load security events."));
    } finally {
      setLoadingEvents(false);
    }
  }, [eventType]);

  useEffect(() => { void loadUsers(); }, [loadUsers]);
  useEffect(() => { void loadEvents(); }, [loadEvents]);

  async function refreshUserSessions(userId: string) {
    setLoadingSessions(userId);
    try {
      const rows = await getAdminUserSessions(userId, true);
      setSessionsByUser((current) => ({ ...current, [userId]: rows }));
    } catch (error) {
      setNotice({ tone: "error", message: errorMessage(error, "Could not load user sessions.") });
    } finally {
      setLoadingSessions(null);
    }
  }

  async function toggleSessions(userId: string) {
    if (expandedUserId === userId) {
      setExpandedUserId(null);
      return;
    }
    setExpandedUserId(userId);
    await refreshUserSessions(userId);
  }

  async function changeUser(
    user: AdminUser,
    update: { role?: DashboardRole; status?: DashboardUserStatus },
  ) {
    const key = `${user.user_id}:${update.role ? "role" : "status"}`;
    setBusyAction(key);
    setNotice(null);
    try {
      const updated = await updateAdminUser(user.user_id, update);
      setUsers((current) => current.map((item) => (
        item.user_id === user.user_id ? { ...item, ...updated } : item
      )));
      if (session?.user_id === user.user_id) {
        await refreshSession();
      } else {
        await loadUsers();
      }
      const message = update.status === "active" && updated.status === "pending_verification"
        ? `${user.display_name} remains pending until their corporate email is verified.`
        : `${user.display_name}'s access was updated.`;
      setNotice({ tone: "success", message });
    } catch (error) {
      setNotice({ tone: "error", message: errorMessage(error, "Could not update user access.") });
    } finally {
      setBusyAction(null);
    }
  }

  async function sendPasswordReset(user: AdminUser) {
    const key = `password-reset:${user.user_id}`;
    setBusyAction(key);
    setNotice(null);
    try {
      const result = await sendAdminPasswordReset(user.user_id);
      setNotice(result.delivered
        ? { tone: "success", message: `Password reset email sent to ${user.email}.` }
        : {
            tone: "error",
            message: "The reset request was created, but email delivery failed. Check the configured authentication mail backend.",
          });
    } catch (error) {
      setNotice({ tone: "error", message: errorMessage(error, "Could not send a password reset email.") });
    } finally {
      setBusyAction(null);
    }
  }

  async function revokeSession(userId: string, sessionId: string) {
    const key = `session:${sessionId}`;
    setBusyAction(key);
    setNotice(null);
    try {
      await revokeAdminSession(sessionId);
      await Promise.all([refreshUserSessions(userId), loadUsers()]);
      if (session?.session_id === sessionId) await refreshSession();
      setNotice({ tone: "success", message: "The selected session was revoked." });
    } catch (error) {
      setNotice({ tone: "error", message: errorMessage(error, "Could not revoke the session.") });
    } finally {
      setBusyAction(null);
    }
  }

  async function bindTelegram(user: AdminUser) {
    const telegramUserId = (telegramDrafts[user.user_id] ?? user.telegram_user_id ?? "").trim();
    const key = `${user.user_id}:telegram`;
    setBusyAction(key);
    setNotice(null);
    try {
      const updated = await bindAdminTelegramIdentity(user.user_id, telegramUserId);
      setUsers((current) => current.map((item) => (item.user_id === updated.user_id ? { ...item, ...updated } : item)));
      setTelegramDrafts((current) => ({ ...current, [user.user_id]: updated.telegram_user_id ?? "" }));
      setNotice({ tone: "success", message: `Telegram id ${updated.telegram_user_id} bound to ${user.display_name}.` });
    } catch (error) {
      setNotice({ tone: "error", message: errorMessage(error, "Could not bind Telegram identity.") });
    } finally {
      setBusyAction(null);
    }
  }

  async function unbindTelegram(user: AdminUser) {
    const key = `${user.user_id}:telegram-unbind`;
    setBusyAction(key);
    setNotice(null);
    try {
      const updated = await unbindAdminTelegramIdentity(user.user_id);
      setUsers((current) => current.map((item) => (item.user_id === updated.user_id ? { ...item, ...updated, telegram_user_id: null } : item)));
      setTelegramDrafts((current) => ({ ...current, [user.user_id]: "" }));
      setNotice({ tone: "success", message: `Telegram identity removed from ${user.display_name}.` });
    } catch (error) {
      setNotice({ tone: "error", message: errorMessage(error, "Could not unbind Telegram identity.") });
    } finally {
      setBusyAction(null);
    }
  }

  async function revokeAll(user: AdminUser) {
    if (!window.confirm(`Revoke all active sessions for ${user.display_name}?`)) return;
    const key = `all:${user.user_id}`;
    setBusyAction(key);
    setNotice(null);
    try {
      const result = await revokeAllAdminUserSessions(user.user_id);
      await Promise.all([refreshUserSessions(user.user_id), loadUsers()]);
      if (session?.user_id === user.user_id) await refreshSession();
      setNotice({
        tone: "success",
        message: `${result.revoked_sessions} session${result.revoked_sessions === 1 ? "" : "s"} revoked.`,
      });
    } catch (error) {
      setNotice({ tone: "error", message: errorMessage(error, "Could not revoke user sessions.") });
    } finally {
      setBusyAction(null);
    }
  }

  function applyUserSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setUserSearch(searchInput.trim());
  }

  function applyEventFilter(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setEventType(eventTypeInput.trim());
  }

  return (
    <div className="mx-auto max-w-[1700px] space-y-6">
      <PageHeader
        eyebrow="Administration"
        title="Admin console"
        description="Manage corporate accounts, Telegram bindings, roles, verification status, browser sessions, security events, reminder delivery reviews, and uncertain draft email reconciliation."
        metadata={<>{users.length} user{users.length === 1 ? "" : "s"} <span aria-hidden="true">•</span> {securityEvents.length} recent security event{securityEvents.length === 1 ? "" : "s"}</>}
        actions={<Badge variant="outline">Admin only</Badge>}
      />

      {notice ? (
        <div
          role={notice.tone === "error" ? "alert" : "status"}
          className={cn(
            "rounded-xl border px-4 py-3 text-sm",
            notice.tone === "error"
              ? "border-danger/30 bg-danger/10 text-danger"
              : "border-success/30 bg-success/10 text-success",
          )}
        >
          {notice.message}
        </div>
      ) : null}

      <Tabs defaultValue="users">
        <TabsList aria-label="Admin console sections" className="max-w-full overflow-x-auto">
          <TabsTrigger value="users"><UserRoundCog className="size-4" />Users</TabsTrigger>
          <TabsTrigger value="security"><KeyRound className="size-4" />Security Events</TabsTrigger>
          <TabsTrigger value="reminders"><BellRing className="size-4" />Reminder Reviews</TabsTrigger>
          {hasPermission("admin.security") ? <TabsTrigger value="draft-outbox"><MailQuestionMark className="size-4" />Draft Outbox</TabsTrigger> : null}
        </TabsList>

        <TabsContent value="users" className="space-y-4 pt-2">
          <Card>
            <CardHeader className="gap-4 sm:flex-row sm:items-center sm:justify-between">
              <div>
                <CardTitle>Dashboard users</CardTitle>
                <p className="mt-1 text-sm text-muted-foreground">
                  Employees register with an exact @demo.invalid email, verify ownership, and then sign in with a local password.
                </p>
              </div>
              <form onSubmit={applyUserSearch} className="flex w-full gap-2 sm:w-auto" role="search">
                <label className="sr-only" htmlFor="user-search">Search users</label>
                <Input
                  id="user-search"
                  value={searchInput}
                  onChange={(event) => setSearchInput(event.target.value)}
                  placeholder="Name or email"
                  className="min-w-0 flex-1 sm:w-64"
                />
                <Button type="submit" size="sm"><Search className="size-4" />Search</Button>
                <Button type="button" size="icon-sm" variant="outline" onClick={() => void loadUsers()} aria-label="Refresh users">
                  <RefreshCw className={cn("size-4", loadingUsers && "animate-spin")} />
                </Button>
              </form>
            </CardHeader>
            <CardContent className="p-0">
              {usersError ? (
                <ErrorState description={usersError} onAction={loadUsers} className="min-h-56 justify-center" />
              ) : loadingUsers && users.length === 0 ? (
                <p className="p-8 text-center text-sm text-muted-foreground" role="status">Loading users...</p>
              ) : users.length === 0 ? (
                <p className="p-8 text-center text-sm text-muted-foreground">No users matched this search.</p>
              ) : (
                <Table className="min-w-[1420px]">
                  <TableHeader>
                    <TableRow>
                      <TableHead>User</TableHead>
                      <TableHead>Account</TableHead>
                      <TableHead>Telegram</TableHead>
                      <TableHead>Role</TableHead>
                      <TableHead>Status</TableHead>
                      <TableHead>Email verification</TableHead>
                      <TableHead>Last login</TableHead>
                      <TableHead>Sessions</TableHead>
                      <TableHead className="text-right">Manage</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {users.map((user) => {
                      const local = user.account_type === "local";
                      const verified = Boolean(user.email_verified_at);
                      const canSendPasswordReset = local && user.status === "active" && verified && Boolean(user.email);
                      const expanded = expandedUserId === user.user_id;
                      const userSessions = sessionsByUser[user.user_id] ?? [];
                      return [
                        <TableRow key={user.user_id}>
                          <TableCell>
                            <div className="font-medium">{user.display_name}</div>
                            <div className="max-w-64 truncate text-xs text-muted-foreground" title={user.email ?? user.user_id}>
                              {user.email ?? user.user_id}
                            </div>
                            {session?.user_id === user.user_id ? <Badge variant="outline" className="mt-1">You</Badge> : null}
                          </TableCell>
                          <TableCell>
                            <div className="flex flex-wrap gap-1">
                              <Badge variant={local ? "outline" : "secondary"}>
                                {local ? "Local account" : "External account"}
                              </Badge>
                              {Boolean(user.is_primary_admin) ? <Badge variant="outline">Primary</Badge> : null}
                            </div>
                          </TableCell>
                          <TableCell>
                            <div className="flex min-w-56 flex-col gap-2">
                              <label className="sr-only" htmlFor={`telegram-${user.user_id}`}>
                                Telegram user id for {user.display_name}
                              </label>
                              <Input
                                id={`telegram-${user.user_id}`}
                                value={telegramDrafts[user.user_id] ?? user.telegram_user_id ?? ""}
                                onChange={(event) => {
                                  const next = event.target.value;
                                  setTelegramDrafts((current) => ({ ...current, [user.user_id]: next }));
                                }}
                                placeholder="Telegram numeric id"
                                inputMode="numeric"
                                className="h-9 font-mono text-xs"
                              />
                              <div className="flex flex-wrap gap-1">
                                <Button
                                  type="button"
                                  size="sm"
                                  variant="outline"
                                  onClick={() => void bindTelegram(user)}
                                  disabled={busyAction === `${user.user_id}:telegram`}
                                >
                                  <Smartphone className="size-3.5" />
                                  Bind
                                </Button>
                                <Button
                                  type="button"
                                  size="sm"
                                  variant="ghost"
                                  onClick={() => void unbindTelegram(user)}
                                  disabled={!user.telegram_user_id || busyAction === `${user.user_id}:telegram-unbind`}
                                >
                                  Unbind
                                </Button>
                              </div>
                            </div>
                          </TableCell>
                          <TableCell>
                            <Select
                              value={user.role}
                              onValueChange={(value) => {
                                if (value && value !== user.role) void changeUser(user, { role: value as DashboardRole });
                              }}
                              disabled={busyAction === `${user.user_id}:role`}
                            >
                              <SelectTrigger aria-label={`Role for ${user.display_name}`} className="w-28"><SelectValue /></SelectTrigger>
                              <SelectContent>
                                <SelectItem value="Viewer">Viewer</SelectItem>
                                <SelectItem value="Editor">Editor</SelectItem>
                                <SelectItem value="Admin">Admin</SelectItem>
                              </SelectContent>
                            </Select>
                          </TableCell>
                          <TableCell>
                            <Select
                              value={user.status}
                              onValueChange={(value) => {
                                if (value && value !== user.status) void changeUser(user, { status: value as DashboardUserStatus });
                              }}
                              disabled={busyAction === `${user.user_id}:status`}
                            >
                              <SelectTrigger aria-label={`Status for ${user.display_name}`} className="w-40"><SelectValue /></SelectTrigger>
                              <SelectContent>
                                <SelectItem value="active">Active</SelectItem>
                                <SelectItem value="pending_verification" disabled>Pending verification</SelectItem>
                                <SelectItem value="disabled">Disabled</SelectItem>
                              </SelectContent>
                            </Select>
                          </TableCell>
                          <TableCell>
                            {local ? verified ? (
                              <span className="inline-flex items-center gap-1.5 text-xs font-medium text-success" title={formatDate(user.email_verified_at)}>
                                <MailCheck className="size-4" />Verified
                              </span>
                            ) : (
                              <span className="inline-flex items-center gap-1.5 text-xs font-medium text-warning">
                                <MailWarning className="size-4" />Waiting
                              </span>
                            ) : (
                              <span className="text-xs text-muted-foreground">Not required</span>
                            )}
                          </TableCell>
                          <TableCell className="text-xs text-muted-foreground">{formatDate(user.last_login_at)}</TableCell>
                          <TableCell><Badge variant="outline">{user.active_session_count ?? 0} active</Badge></TableCell>
                          <TableCell>
                            <div className="flex flex-wrap justify-end gap-2">
                              {canSendPasswordReset ? (
                                <Button
                                  size="sm"
                                  variant="outline"
                                  onClick={() => void sendPasswordReset(user)}
                                  disabled={busyAction === `password-reset:${user.user_id}`}
                                >
                                  <Send className="size-4" />
                                  {busyAction === `password-reset:${user.user_id}` ? "Sending..." : "Password reset"}
                                </Button>
                              ) : null}
                              <Button size="sm" variant="outline" onClick={() => void toggleSessions(user.user_id)} aria-expanded={expanded}>
                                <Smartphone className="size-4" />Sessions {expanded ? <ChevronUp className="size-4" /> : <ChevronDown className="size-4" />}
                              </Button>
                              <Button
                                size="sm"
                                variant="destructive"
                                onClick={() => void revokeAll(user)}
                                disabled={(user.active_session_count ?? 0) === 0 || busyAction === `all:${user.user_id}`}
                              >
                                Revoke all
                              </Button>
                            </div>
                          </TableCell>
                        </TableRow>,
                        expanded ? (
                          <TableRow key={`${user.user_id}:sessions`} className="bg-muted/20 hover:bg-muted/20">
                            <TableCell colSpan={9} className="p-4">
                              <UserSessions
                                sessions={userSessions}
                                loading={loadingSessions === user.user_id}
                                busyAction={busyAction}
                                onRefresh={() => void refreshUserSessions(user.user_id)}
                                onRevoke={(sessionId) => void revokeSession(user.user_id, sessionId)}
                              />
                            </TableCell>
                          </TableRow>
                        ) : null,
                      ];
                    })}
                  </TableBody>
                </Table>
              )}
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="security" className="space-y-4 pt-2">
          <Card>
            <CardHeader className="gap-4 sm:flex-row sm:items-center sm:justify-between">
              <div>
                <CardTitle>Security events</CardTitle>
                <p className="mt-1 text-sm text-muted-foreground">Newest first. Event metadata is sanitized before storage.</p>
              </div>
              <form onSubmit={applyEventFilter} className="flex w-full gap-2 sm:w-auto" role="search">
                <label className="sr-only" htmlFor="event-filter">Filter event type</label>
                <input
                  id="event-filter"
                  value={eventTypeInput}
                  onChange={(event) => setEventTypeInput(event.target.value)}
                  placeholder="Exact event type"
                  className="h-10 min-w-0 flex-1 rounded-lg border bg-background px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring sm:w-64"
                />
                <Button type="submit" size="sm"><Search className="size-4" />Filter</Button>
                <Button type="button" size="icon-sm" variant="outline" onClick={() => void loadEvents()} aria-label="Refresh security events">
                  <RefreshCw className={cn("size-4", loadingEvents && "animate-spin")} />
                </Button>
              </form>
            </CardHeader>
            <CardContent className="space-y-3">
              {eventsError ? (
                <ErrorState description={eventsError} onAction={loadEvents} className="min-h-56 justify-center" />
              ) : loadingEvents && securityEvents.length === 0 ? (
                <p className="py-8 text-center text-sm text-muted-foreground" role="status">Loading security events...</p>
              ) : securityEvents.length === 0 ? (
                <p className="py-8 text-center text-sm text-muted-foreground">No security events matched this filter.</p>
              ) : securityEvents.map((event) => (
                <article key={event.event_id} className="rounded-xl border p-4">
                  <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className={cn("inline-flex h-6 items-center rounded-full border px-2 text-xs font-medium", severityClass(event.severity))}>
                          {event.severity}
                        </span>
                        <h3 className="break-all font-mono text-sm font-semibold">{event.event_type}</h3>
                      </div>
                      <p className="mt-2 text-xs text-muted-foreground">{formatDate(event.created_at)}</p>
                    </div>
                    <div className="grid gap-1 text-xs text-muted-foreground sm:text-right">
                      <span>Actor: {event.actor_user_id ?? "system"}</span>
                      <span>Subject: {event.subject_user_id ?? "none"}</span>
                      <span>Session: {event.session_id ?? "none"}</span>
                    </div>
                  </div>
                  <details className="mt-3 rounded-lg bg-muted/40 px-3 py-2">
                    <summary className="cursor-pointer text-xs font-medium">Metadata and request context</summary>
                    <pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap break-all text-xs text-muted-foreground">
                      {JSON.stringify({ ip_address: event.ip_address, user_agent: event.user_agent, metadata: event.metadata }, null, 2)}
                    </pre>
                  </details>
                </article>
              ))}
            </CardContent>
          </Card>
        </TabsContent>
        <TabsContent value="reminders" className="pt-2">
          <ReminderDeliveries />
        </TabsContent>
        {hasPermission("admin.security") ? (
          <TabsContent value="draft-outbox" className="pt-2">
            <DraftOutboxReconciliation />
          </TabsContent>
        ) : null}
      </Tabs>
    </div>
  );
}
