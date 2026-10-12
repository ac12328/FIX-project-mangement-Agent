"use client";

import { useState } from "react";
import Link from "next/link";
import {
  AlertTriangle,
  ArrowLeft,
  CheckCircle2,
  FolderPlus,
  Loader2,
} from "lucide-react";

import { InfoField } from "@/components/info-field";
import { PageHeader } from "@/components/page-header";
import { ErrorPanel, StatePanel } from "@/components/state-panel";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { createProjectIntake } from "@/lib/api";
import type { IntakeResult } from "@/lib/types";
import { notifyError, notifySuccess } from "@/lib/notifications";
import { cn } from "@/lib/utils";


export default function IntakePage() {
  const [form, setForm] = useState({
    project_id: "",
    client_name: "",
    client_email: "",
    market: "HK",
    connection_type: "FIX 4.2",
    target_go_live: "",
    confirm: false,
    project_tier: "medium" as "high" | "medium" | "low",
    counterparty: undefined as string | undefined,
  });
  const [result, setResult] = useState<IntakeResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const res = await createProjectIntake({
        project_id: form.project_id || undefined,
        client_name: form.client_name,
        client_email: form.client_email || undefined,
        market: form.market || undefined,
        connection_type: form.connection_type,
        target_go_live: form.target_go_live || undefined,
        confirm: form.confirm,
        source: "web_form",
      });
      setResult(res);
      notifySuccess(res.committed ? "Project created" : "Intake preview ready", res.message);
    } catch (e: unknown) {
      const message = e instanceof Error ? e.message : "Intake failed";
      setError(message);
      notifyError(e, message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="mx-auto max-w-[1400px] space-y-6">
      <PageHeader
        eyebrow="Projects"
        title="New project intake"
        description="Preview first, then confirm. Nothing is written until you say so."
        metadata="Preview-first • No writes until confirmation"
        actions={
          <Button variant="outline" render={<Link href="/projects" />}>
            <ArrowLeft className="size-4" />
            Back to projects
          </Button>
        }
      />

      <div className="grid gap-6 lg:grid-cols-[1fr_1fr]">
        {/* Form */}
        <Card className="border border-border/80 bg-card shadow-sm">
          <CardHeader className="pb-2">
            <CardTitle className="text-xs font-medium uppercase tracking-[0.14em] text-muted-foreground">
              Project Details
            </CardTitle>
          </CardHeader>
          <CardContent>
            <form onSubmit={handleSubmit} className="space-y-4">
              <div className="grid gap-3 sm:grid-cols-2">
                <FormField
                  label="Project ID"
                  value={form.project_id}
                  onChange={(v) => setForm((f) => ({ ...f, project_id: v }))}
                  placeholder="e.g. HKG-001"
                />
                <FormField
                  label="Client Name"
                  value={form.client_name}
                  onChange={(v) => setForm((f) => ({ ...f, client_name: v }))}
                  placeholder="e.g. HSBC"
                  required
                />
                <FormField
                  label="Client Email"
                  value={form.client_email}
                  onChange={(v) => setForm((f) => ({ ...f, client_email: v }))}
                  placeholder="client@example.com"
                  type="email"
                />
                <FormField
                  label="Market"
                  value={form.market}
                  onChange={(v) => setForm((f) => ({ ...f, market: v }))}
                  placeholder="HK"
                />
                <FormField
                  label="Connection Type"
                  value={form.connection_type}
                  onChange={(v) => setForm((f) => ({ ...f, connection_type: v }))}
                  placeholder="FIX 4.2"
                />
                <FormField
                  label="Target Go-Live"
                  value={form.target_go_live}
                  onChange={(v) => setForm((f) => ({ ...f, target_go_live: v }))}
                  placeholder="YYYY-MM-DD"
                  type="date"
                />

                {/* Counterparty */}
                <label className="flex flex-col gap-1.5 text-sm">
                  <span className="text-xs font-medium text-muted-foreground">Counterparty</span>
                  <select
                    value={form.counterparty ?? ""}
                    onChange={(e) => setForm((f) => ({ ...f, counterparty: e.target.value || undefined }))}
                    className="rounded-lg border border-border bg-background px-3 py-2 text-sm shadow-sm outline-none transition focus-visible:ring-2 focus-visible:ring-primary/40"
                  >
                    <option value="">Select counterparty...</option>
                    <option value="Demo Gateway A">Demo Gateway A</option>
                    <option value="Demo Gateway B">Demo Gateway B</option>
                    <option value="DBLH">DBLH</option>
                    <option value="LS DB">LS DB</option>
                    <option value="Other">Other</option>
                  </select>
                </label>
              </div>

              {/* Project Tier */}
              <div className="space-y-1.5">
                <span className="text-xs font-medium text-muted-foreground">Project Tier</span>
                <div className="flex gap-2">
                  {(["high", "medium", "low"] as const).map((tier) => (
                    <button
                      key={tier}
                      type="button"
                      onClick={() => setForm((f) => ({ ...f, project_tier: tier }))}
                      className={cn(
                        "min-h-11 flex-1 rounded-lg border px-3 py-2 text-xs font-semibold capitalize transition-colors",
                        form.project_tier === tier
                          ? tier === "high" ? "border-danger/40 bg-danger/10 text-danger"
                            : tier === "medium" ? "border-warning/40 bg-warning/10 text-warning"
                            : "border-success/40 bg-success/10 text-success"
                          : "border-border bg-background text-muted-foreground hover:bg-muted/50"
                      )}
                    >
                      {tier}
                    </button>
                  ))}
                </div>
              </div>

              <label className="flex min-h-11 items-center gap-3 rounded-lg border bg-muted/20 px-3 text-sm">
                <input
                  type="checkbox"
                  checked={form.confirm}
                  onChange={(e) => setForm((f) => ({ ...f, confirm: e.target.checked }))}
                  className="size-4 rounded border-input accent-primary"
                />
                Confirm and commit to database
              </label>

              <div className="flex justify-end">
                <Button
                  type="submit"
                  disabled={loading || !form.client_name}
                >
                  {loading ? (
                    <><Loader2 className="size-4 animate-spin" /> Submitting…</>
                  ) : (
                    <><FolderPlus className="size-4" /> {form.confirm ? "Submit & Commit" : "Preview intake"}</>
                  )}
                </Button>
              </div>
            </form>
          </CardContent>
        </Card>

        {/* Result */}
        <div className="space-y-4">
          {error ? <ErrorPanel description={error} /> : null}

          {result && (
            <Card
              className={cn(
                "border bg-card shadow-sm",
                result.committed
                  ? "border-success/40"
                  : "border-warning/40",
              )}
            >
              <CardHeader className="pb-2">
                <div className="flex items-center gap-2">
                  {result.committed ? (
                    <CheckCircle2 className="size-4 text-success" />
                  ) : (
                    <AlertTriangle className="size-4 text-warning" />
                  )}
                  <CardTitle className="text-sm">
                    {result.committed ? "Project Created" : "Preview (not committed)"}
                  </CardTitle>
                </div>
              </CardHeader>
              <CardContent className="space-y-3">
                <div className="grid grid-cols-2 gap-2">
                  <InfoField label="Project ID" value={result.project_id} mono />
                  <InfoField label="Client" value={result.client_name} />
                  <InfoField label="Email" value={result.client_email ?? "—"} />
                  <InfoField label="Market" value={result.market ?? "—"} />
                  <InfoField label="Connection" value={result.connection_type} />
                  <InfoField
                    label="Confidence"
                    value={`${(result.confidence * 100).toFixed(0)}%`}
                  />
                </div>

                {result.extraction_warnings.length > 0 && (
                  <div className="rounded-lg border border-warning/30 bg-warning/10 p-3">
                    <p className="mb-1 text-xs font-medium uppercase text-warning">
                      Warnings
                    </p>
                    {result.extraction_warnings.map((w, i) => (
                      <p key={i} className="text-xs text-warning">
                        {w}
                      </p>
                    ))}
                  </div>
                )}

                <p className="text-xs text-muted-foreground">{result.message}</p>

                {!result.committed && result.needs_confirmation && (
                  <Button
                    size="sm"
                    className="gap-1.5"
                    onClick={() => {
                      setForm((f) => ({ ...f, confirm: true }));
                      setTimeout(
                        () => document.querySelector("form")?.requestSubmit(),
                        50,
                      );
                    }}
                  >
                    <CheckCircle2 className="size-3" />
                    Confirm &amp; Commit
                  </Button>
                )}
              </CardContent>
            </Card>
          )}

          {!result && !error ? (
            <StatePanel
              icon={FolderPlus}
              title="Ready for intake details"
              description="Complete the form and preview the normalized project record before committing it."
            />
          ) : null}
        </div>
      </div>
    </div>
  );
}

/* ── Sub-components ──────────────────────────────────────────── */

function FormField({
  label,
  value,
  onChange,
  placeholder,
  type = "text",
  required,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  type?: string;
  required?: boolean;
}) {
  return (
    <label className="flex flex-col gap-1.5 text-sm">
      <span className="text-xs font-medium text-muted-foreground">
        {label}
        {required && <span className="text-danger"> *</span>}
      </span>
      <Input
        type={type}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        required={required}
      />
    </label>
  );
}
