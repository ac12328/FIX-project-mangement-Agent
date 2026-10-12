"use client";

import { useCallback, useEffect, useState, type FormEvent } from "react";
import { BellRing, Save, Settings2, Trash2 } from "lucide-react";

import { PageHeader } from "@/components/page-header";
import { ResponsiveList, ResponsiveListItem } from "@/components/responsive-list";
import { SectionPanel } from "@/components/section-panel";
import { ErrorPanel, LoadingPanel, StatePanel } from "@/components/state-panel";
import { StatusBadge } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import {
  createPreference,
  deletePreference,
  fetchNotificationPrefs,
  getPreferences,
  updateNotificationPrefs,
} from "@/lib/api";
import { notifyError, notifySuccess } from "@/lib/notifications";
import type { OperatorPreference } from "@/lib/types";
import { cn } from "@/lib/utils";

type PreferenceForm = {
  scope: "project" | "counterparty";
  project_id: string;
  counterparty_id: string;
  tone: string;
  language: string;
  push_style: string;
  custom_notes: string;
};

const EMPTY: PreferenceForm = {
  scope: "project",
  project_id: "",
  counterparty_id: "",
  tone: "semi-formal",
  language: "bilingual",
  push_style: "reactive",
  custom_notes: "",
};

const fieldClassName = "h-11 w-full rounded-lg border border-input bg-background px-3 text-sm outline-none transition-colors focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50";

export default function PreferencesPage() {
  const [preferences, setPreferences] = useState<OperatorPreference[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [form, setForm] = useState(EMPTY);
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState<number | null>(null);

  const reload = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setPreferences(await getPreferences());
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not load preferences.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void reload(); }, [reload]);

  async function handleSave(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSaving(true);
    setError(null);
    try {
      await createPreference({
        scope: form.scope,
        project_id: form.scope === "project" ? form.project_id.trim() || null : null,
        counterparty_id: form.scope === "counterparty" ? form.counterparty_id.trim() || null : null,
        tone: form.tone.trim(),
        language: form.language.trim(),
        push_style: form.push_style.trim(),
        custom_notes: form.custom_notes.trim() || null,
      });
      setForm(EMPTY);
      notifySuccess("Preference saved", "Future agent responses will use the updated communication guidance.");
      await reload();
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : "Could not save the preference.";
      setError(message);
      notifyError(cause, message);
    } finally {
      setSaving(false);
    }
  }

  async function handleDelete(id: number) {
    if (!window.confirm("Delete this communication preference?")) return;
    setDeleting(id);
    setError(null);
    try {
      await deletePreference(id);
      notifySuccess("Preference deleted");
      await reload();
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : "Could not delete the preference.";
      setError(message);
      notifyError(cause, message);
    } finally {
      setDeleting(null);
    }
  }

  return (
    <div className="mx-auto max-w-[1500px] space-y-6">
      <PageHeader
        eyebrow="Account"
        title="Preferences"
        description="Configure personal communication guidance and notification delivery without changing shared project truth."
        metadata={`${preferences.length} saved preference${preferences.length === 1 ? "" : "s"}`}
      />

      {error ? <ErrorPanel description={error} onRetry={() => void reload()} /> : null}

      <div className="grid gap-6 xl:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)]">
        <SectionPanel
          title="Communication guidance"
          description="Project or counterparty-specific tone, language, and follow-up behavior used by the assistant."
        >
          <form onSubmit={handleSave} className="grid gap-4 pt-5 sm:grid-cols-2">
            <label className="space-y-1.5 text-sm font-medium">
              <span>Scope</span>
              <Select
                value={form.scope}
                onValueChange={(value) => {
                  if (value === "project" || value === "counterparty") {
                    setForm((current) => ({ ...current, scope: value }));
                  }
                }}
              >
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="project">Project</SelectItem>
                  <SelectItem value="counterparty">Counterparty</SelectItem>
                </SelectContent>
              </Select>
            </label>

            <label className="space-y-1.5 text-sm font-medium">
              <span>{form.scope === "project" ? "Project ID" : "Counterparty ID"}</span>
              <Input
                value={form.scope === "project" ? form.project_id : form.counterparty_id}
                onChange={(event) => setForm((current) => ({
                  ...current,
                  [form.scope === "project" ? "project_id" : "counterparty_id"]: event.target.value,
                }))}
                placeholder={form.scope === "project" ? "e.g. P-001" : "e.g. ORG-001"}
              />
            </label>

            <label className="space-y-1.5 text-sm font-medium">
              <span>Tone</span>
              <Input value={form.tone} onChange={(event) => setForm((current) => ({ ...current, tone: event.target.value }))} />
            </label>

            <label className="space-y-1.5 text-sm font-medium">
              <span>Language</span>
              <Input value={form.language} onChange={(event) => setForm((current) => ({ ...current, language: event.target.value }))} />
            </label>

            <label className="space-y-1.5 text-sm font-medium sm:col-span-2">
              <span>Push style</span>
              <Input value={form.push_style} onChange={(event) => setForm((current) => ({ ...current, push_style: event.target.value }))} />
            </label>

            <label className="space-y-1.5 text-sm font-medium sm:col-span-2">
              <span>Custom notes</span>
              <Textarea
                value={form.custom_notes}
                onChange={(event) => setForm((current) => ({ ...current, custom_notes: event.target.value }))}
                className="min-h-24"
              />
            </label>

            <div className="sm:col-span-2">
              <Button type="submit" disabled={saving}>
                <Save className="size-4" />
                {saving ? "Saving..." : "Save preference"}
              </Button>
            </div>
          </form>
        </SectionPanel>

        <SectionPanel
          title="Saved preferences"
          description="More specific guidance is applied when its project or counterparty matches the current context."
        >
          <div className="pt-5">
            {loading ? (
              <LoadingPanel label="Loading communication preferences" />
            ) : preferences.length === 0 ? (
              <StatePanel
                icon={Settings2}
                title="No preferences saved"
                description="Add a preference to customize tone, language, or follow-up behavior."
              />
            ) : (
              <ResponsiveList ariaLabel="Saved communication preferences">
                {preferences.map((preference) => (
                  <ResponsiveListItem
                    key={preference.id ?? `${preference.scope}-${preference.project_id}-${preference.counterparty_id}`}
                    title={preference.project_id ?? preference.counterparty_id ?? "Default guidance"}
                    metadata={`Updated ${preference.updated_at ? new Date(preference.updated_at).toLocaleString() : "date unavailable"}`}
                    status={<StatusBadge value={preference.scope} />}
                    action={preference.id !== null ? (
                      <Button
                        variant="outline"
                        size="sm"
                        className="text-destructive hover:bg-destructive/10 hover:text-destructive"
                        disabled={deleting === preference.id}
                        onClick={() => void handleDelete(preference.id!)}
                      >
                        <Trash2 className="size-4" />
                        {deleting === preference.id ? "Deleting..." : "Delete"}
                      </Button>
                    ) : undefined}
                  >
                    <dl className="grid gap-3 sm:grid-cols-2">
                      <PreferenceValue label="Tone" value={preference.tone} />
                      <PreferenceValue label="Language" value={preference.language} />
                      <PreferenceValue label="Push style" value={preference.push_style} />
                      {preference.custom_notes ? <PreferenceValue label="Notes" value={preference.custom_notes} /> : null}
                    </dl>
                  </ResponsiveListItem>
                ))}
              </ResponsiveList>
            )}
          </div>
        </SectionPanel>
      </div>

      <NotificationBudgetSection />
    </div>
  );
}

function PreferenceValue({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-xs font-medium text-muted-foreground">{label}</dt>
      <dd className="mt-1 text-sm text-foreground">{value}</dd>
    </div>
  );
}

const DAILY_LIMIT_OPTIONS = [
  { label: "5", value: 5 },
  { label: "10", value: 10 },
  { label: "15", value: 15 },
  { label: "25", value: 25 },
  { label: "Unlimited", value: 0 },
];

const DELIVERY_MODE_OPTIONS = [
  { label: "Realtime", value: "realtime" },
  { label: "3x daily", value: "3x_daily" },
  { label: "2x daily", value: "2x_daily" },
];

type NotificationMode = "active" | "silent" | "emergency";
interface NotificationPreferences {
  max_daily: number;
  delivery_mode: string;
  quiet_hours_start: string;
  quiet_hours_end: string;
}

const DEFAULT_NOTIFICATIONS: NotificationPreferences = {
  max_daily: 0,
  delivery_mode: "realtime",
  quiet_hours_start: "22:00",
  quiet_hours_end: "08:00",
};

function NotificationBudgetSection() {
  const [preferences, setPreferences] = useState(DEFAULT_NOTIFICATIONS);
  const [mode, setMode] = useState<NotificationMode>("active");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const data = await fetchNotificationPrefs();
      setPreferences({
        max_daily: typeof data.max_daily === "number" ? data.max_daily : DEFAULT_NOTIFICATIONS.max_daily,
        delivery_mode: typeof data.delivery_mode === "string" ? data.delivery_mode : DEFAULT_NOTIFICATIONS.delivery_mode,
        quiet_hours_start: typeof data.quiet_hours_start === "string" ? data.quiet_hours_start : DEFAULT_NOTIFICATIONS.quiet_hours_start,
        quiet_hours_end: typeof data.quiet_hours_end === "string" ? data.quiet_hours_end : DEFAULT_NOTIFICATIONS.quiet_hours_end,
      });
      if (data.notification_mode === "active" || data.notification_mode === "silent" || data.notification_mode === "emergency") {
        setMode(data.notification_mode);
      }
    } catch (cause) {
      setLoadError(cause instanceof Error ? cause.message : "Could not load notification preferences.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  async function handleSave(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSaving(true);
    try {
      await updateNotificationPrefs({ ...preferences, notification_mode: mode });
      notifySuccess("Notification preferences saved");
    } catch (cause) {
      notifyError(cause, "Could not save notification preferences.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <SectionPanel
      title={<span className="inline-flex items-center gap-2"><BellRing className="size-4 text-primary" />Notification delivery</span>}
      description="Control volume, delivery cadence, and quiet hours for personal Dashboard notifications."
    >
      <div className="pt-5">
        {loadError ? <ErrorPanel description={loadError} onRetry={() => void load()} /> : null}
        {loading ? <LoadingPanel label="Loading notification preferences" /> : (
          <form onSubmit={handleSave} className="space-y-5">
            <fieldset>
              <legend className="text-sm font-medium">Notification mode</legend>
              <div className="mt-2 grid gap-2 md:grid-cols-3">
                {([
                  { value: "active", label: "Active", description: "Deliver all eligible alerts immediately." },
                  { value: "silent", label: "Silent", description: "Hold non-critical notifications." },
                  { value: "emergency", label: "Emergency only", description: "Deliver critical operational alerts only." },
                ] as const).map((option) => (
                  <button
                    key={option.value}
                    type="button"
                    aria-pressed={mode === option.value}
                    onClick={() => setMode(option.value)}
                    className={cn(
                      "min-h-20 rounded-xl border p-3 text-left outline-none transition-colors focus-visible:ring-3 focus-visible:ring-ring/50",
                      mode === option.value ? "border-primary bg-primary/10" : "bg-background hover:bg-muted/50",
                    )}
                  >
                    <span className="block text-sm font-semibold">{option.label}</span>
                    <span className="mt-1 block text-xs leading-5 text-muted-foreground">{option.description}</span>
                  </button>
                ))}
              </div>
            </fieldset>

            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
              <label className="space-y-1.5 text-sm font-medium">
                <span>Max daily notifications</span>
                <select
                  value={preferences.max_daily}
                  onChange={(event) => setPreferences((current) => ({ ...current, max_daily: Number(event.target.value) }))}
                  className={fieldClassName}
                >
                  {DAILY_LIMIT_OPTIONS.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
                </select>
              </label>
              <label className="space-y-1.5 text-sm font-medium">
                <span>Delivery mode</span>
                <select
                  value={preferences.delivery_mode}
                  onChange={(event) => setPreferences((current) => ({ ...current, delivery_mode: event.target.value }))}
                  className={fieldClassName}
                >
                  {DELIVERY_MODE_OPTIONS.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
                </select>
              </label>
              <label className="space-y-1.5 text-sm font-medium">
                <span>Quiet hours start</span>
                <Input type="time" value={preferences.quiet_hours_start} onChange={(event) => setPreferences((current) => ({ ...current, quiet_hours_start: event.target.value }))} />
              </label>
              <label className="space-y-1.5 text-sm font-medium">
                <span>Quiet hours end</span>
                <Input type="time" value={preferences.quiet_hours_end} onChange={(event) => setPreferences((current) => ({ ...current, quiet_hours_end: event.target.value }))} />
              </label>
            </div>

            <Button type="submit" disabled={saving}>
              <Save className="size-4" />
              {saving ? "Saving..." : "Save notification preferences"}
            </Button>
          </form>
        )}
      </div>
    </SectionPanel>
  );
}
