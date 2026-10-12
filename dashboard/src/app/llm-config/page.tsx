"use client";

import { useEffect, useState } from "react";
import {
  Check,
  RotateCcw,
  Save,
  Server,
} from "lucide-react";

import { PageHeader } from "@/components/page-header";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import {
  getLLMConfig,
  updateLLMConfig,
  getLLMRouting,
  updateLLMRouting,
  type LLMConfig,
  type TaskRoute,
} from "@/lib/api";
import { notifyError, notifySuccess } from "@/lib/notifications";
import { cn } from "@/lib/utils";

type Provider =
  | "openrouter"
  | "openai"
  | "anthropic"
  | "gemini"
  | "vertex_ai"
  | "minimax"
  | "ollama"
  | "custom";

const OLLAMA_DEFAULT_BASE = "http://llm.demo.invalid/v1";
const OLLAMA_DEFAULT_MODEL = "glm-5.2:cloud";

const PROVIDERS: { id: Provider; label: string; description: string }[] = [
  { id: "openrouter", label: "OpenRouter", description: "Multi-provider gateway" },
  { id: "openai", label: "OpenAI", description: "GPT-4o, GPT-4-turbo" },
  { id: "anthropic", label: "Anthropic", description: "Claude 4, Claude 3.5" },
  { id: "gemini", label: "Google Gemini", description: "Gemini 2.0 Flash / Pro" },
  { id: "vertex_ai", label: "Vertex AI", description: "Google Cloud AI" },
  { id: "minimax", label: "MiniMax", description: "MiniMax M2.7" },
  { id: "ollama", label: "Ollama", description: "GLM-5.2 cloud, local models" },
  { id: "custom", label: "Custom Endpoint", description: "LiteLLM, LM Studio" },
];

const MODEL_SUGGESTIONS: Record<Provider, string[]> = {
  openrouter: [
    "google/gemini-2.0-flash-001",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "anthropic/claude-3.5-sonnet",
    "openai/gpt-4o",
  ],
  openai: ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-3.5-turbo"],
  anthropic: [
    "claude-sonnet-4-20250514",
    "claude-3-5-haiku-20241022",
    "claude-3-5-sonnet-20241022",
  ],
  gemini: ["gemini-2.0-flash", "gemini-1.5-pro", "gemini-1.5-flash"],
  vertex_ai: ["gemini-2.0-flash", "gemini-1.5-pro"],
  minimax: ["MiniMax-M2.7", "MiniMax-M2.7-highspeed", "MiniMax-Text-01"],
  ollama: [
    "glm-5.2:cloud",
    "glm-5.1:cloud",
    "gemma4:31b-cloud",
    "kimi-k2.6:cloud",
    "deepseek-v4-flash:cloud",
  ],
  custom: [],
};

type FormState = Omit<LLMConfig, "active_provider" | "active_model">;

const EMPTY_FORM: FormState = {
  openrouter_api_key: "",
  openai_api_key: "",
  anthropic_api_key: "",
  gemini_api_key: "",
  minimax_api_key: "",
  ollama_api_base: OLLAMA_DEFAULT_BASE,
  ollama_api_key: "",
  local_llm_api_base: "",
  local_llm_api_key: "",
  local_llm_model: "",
  vertex_ai_project: "",
  vertex_ai_location: "us-central1",
  vertex_ai_model: "",
  vertex_ai_credentials_path: "",
};

export default function LLMConfigPage() {
  const [provider, setProvider] = useState<Provider | "">("");
  const [model, setModel] = useState("");
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);

  useEffect(() => {
    getLLMConfig()
      .then((data) => {
        const { active_provider, active_model, ...rest } = data;
        setProvider((active_provider || "") as Provider | "");
        setModel(active_model || "");
        setForm(rest);
      })
      .catch((e: Error) => setMsg({ kind: "err", text: e.message }))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    if (!msg) return;
    const id = window.setTimeout(() => setMsg(null), 5000);
    return () => window.clearTimeout(id);
  }, [msg]);

  const set =
    (field: keyof FormState) =>
    (e: React.ChangeEvent<HTMLInputElement>) =>
      setForm((f) => ({ ...f, [field]: e.target.value }));

  const handleProviderSelect = (next: Provider) => {
    setProvider(next);
    if (next === "ollama") {
      setModel((current) => current || OLLAMA_DEFAULT_MODEL);
      setForm((f) => ({
        ...f,
        ollama_api_base: f.ollama_api_base || OLLAMA_DEFAULT_BASE,
        ollama_api_key: f.ollama_api_key || "",
      }));
    }
  };

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!provider) {
      setMsg({ kind: "err", text: "Select a provider first" });
      return;
    }
    if (!model) {
      setMsg({ kind: "err", text: "Enter a model name" });
      return;
    }
    setSaving(true);
    setMsg(null);
    try {
      const res = await updateLLMConfig({
        active_provider: provider,
        active_model: model,
        ...form,
      });
      const message = `Saved. Provider: ${res.provider}, Model: ${res.model}`;
      setMsg({ kind: "ok", text: message });
      notifySuccess("LLM configuration saved", `${res.provider} · ${res.model}`);
    } catch (err) {
      const message = err instanceof Error ? err.message : "Save failed";
      setMsg({ kind: "err", text: message });
      notifyError(err, message);
    } finally {
      setSaving(false);
    }
  };

  if (loading) {
    return (
      <div className="mx-auto max-w-[1600px] space-y-6">
        <PageHeader
          eyebrow="Administration"
          title="LLM configuration"
          description="Loading provider, model, credentials, and task routing settings."
        />
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} className="h-20 w-full" />
          ))}
        </div>
      </div>
    );
  }

  const suggestions = provider ? MODEL_SUGGESTIONS[provider] : [];

  return (
    <div className="mx-auto max-w-[1600px] space-y-6">
      <PageHeader
        eyebrow="Administration"
        title="LLM configuration"
        description="Select the active provider and model, manage credentials, and configure per-task routing. Changes take effect immediately."
        metadata={provider && model ? <>Active: <span className="font-mono text-foreground">{provider} · {model}</span></> : "No active provider selected"}
      />

      {msg && (
        <div
          className={cn(
            "rounded-lg border px-4 py-2.5 text-sm",
            msg.kind === "ok"
              ? "border-success/30 bg-success/10 text-success"
              : "border-danger/30 bg-danger/10 text-danger",
          )}
        >
          {msg.text}
        </div>
      )}

      {/* Provider selector */}
      <Card className="border border-border/80 bg-card shadow-sm">
        <CardHeader className="pb-2">
          <CardTitle className="text-xs font-medium uppercase tracking-[0.14em] text-muted-foreground">
            Provider
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            {PROVIDERS.map((p) => (
              <button
                key={p.id}
                type="button"
                onClick={() => handleProviderSelect(p.id)}
                className={cn(
                  "cursor-pointer rounded-lg border px-3 py-2.5 text-left transition-all",
                  provider === p.id
                    ? "border-primary bg-primary/10 shadow-sm ring-1 ring-primary/30"
                    : "border-border hover:bg-muted/30",
                )}
              >
                <div className="text-sm font-medium">{p.label}</div>
                <div className="mt-0.5 text-xs text-muted-foreground">
                  {p.description}
                </div>
              </button>
            ))}
          </div>
        </CardContent>
      </Card>

      {/* Model + Credentials */}
      {provider && (
        <form onSubmit={handleSave}>
          <Card className="border border-border/80 bg-card shadow-sm">
            <CardHeader className="pb-2">
              <CardTitle className="text-xs font-medium uppercase tracking-[0.14em] text-muted-foreground">
                Model &amp; Credentials
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              {/* Model selector */}
              <div className="space-y-2">
                <label className="text-xs font-medium text-muted-foreground">
                  Model
                </label>
                <input
                  type="text"
                  value={model}
                  onChange={(e) => setModel(e.target.value)}
                  placeholder="Enter model name"
                  className="w-full rounded-lg border border-border bg-background px-3 py-2 font-mono text-sm shadow-sm outline-none transition focus-visible:ring-2 focus-visible:ring-primary/40"
                  list="model-suggestions"
                />
                {suggestions.length > 0 && (
                  <datalist id="model-suggestions">
                    {suggestions.map((s) => (
                      <option key={s} value={s} />
                    ))}
                  </datalist>
                )}
                {suggestions.length > 0 && (
                  <div className="flex flex-wrap gap-1.5">
                    {suggestions.map((s) => (
                      <button
                        key={s}
                        type="button"
                        onClick={() => setModel(s)}
                        className={cn(
                          "cursor-pointer rounded-md border px-2 py-0.5 text-xs transition-colors",
                          model === s
                            ? "border-primary bg-primary/10 text-primary"
                            : "border-border text-muted-foreground hover:bg-muted/30",
                        )}
                      >
                        {s}
                      </button>
                    ))}
                  </div>
                )}
              </div>

              {/* Provider-specific fields */}
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                {provider === "openrouter" && (
                  <Field
                    label="OpenRouter API Key"
                    value={form.openrouter_api_key}
                    onChange={set("openrouter_api_key")}
                    placeholder="sk-or-..."
                    secret
                    className="sm:col-span-2"
                  />
                )}
                {provider === "openai" && (
                  <Field
                    label="OpenAI API Key"
                    value={form.openai_api_key}
                    onChange={set("openai_api_key")}
                    placeholder="sk-..."
                    secret
                    className="sm:col-span-2"
                  />
                )}
                {provider === "anthropic" && (
                  <Field
                    label="Anthropic API Key"
                    value={form.anthropic_api_key}
                    onChange={set("anthropic_api_key")}
                    placeholder="sk-ant-..."
                    secret
                    className="sm:col-span-2"
                  />
                )}
                {provider === "gemini" && (
                  <Field
                    label="Gemini API Key"
                    value={form.gemini_api_key}
                    onChange={set("gemini_api_key")}
                    placeholder="AIza..."
                    secret
                    className="sm:col-span-2"
                  />
                )}
                {provider === "vertex_ai" && (
                  <>
                    <Field
                      label="GCP Project ID"
                      value={form.vertex_ai_project}
                      onChange={set("vertex_ai_project")}
                      placeholder="my-gcp-project"
                    />
                    <Field
                      label="Location"
                      value={form.vertex_ai_location}
                      onChange={set("vertex_ai_location")}
                      placeholder="us-central1"
                    />
                    <Field
                      label="Credentials JSON Path"
                      value={form.vertex_ai_credentials_path}
                      onChange={set("vertex_ai_credentials_path")}
                      placeholder="/path/to/service_account.json"
                      className="sm:col-span-2"
                    />
                  </>
                )}
                {provider === "minimax" && (
                  <Field
                    label="MiniMax API Key"
                    value={form.minimax_api_key}
                    onChange={set("minimax_api_key")}
                    placeholder="eyJ..."
                    secret
                    className="sm:col-span-2"
                  />
                )}
                {provider === "ollama" && (
                  <>
                    <Field
                      label="Ollama API Base URL"
                      value={form.ollama_api_base}
                      onChange={set("ollama_api_base")}
                      placeholder={OLLAMA_DEFAULT_BASE}
                      className="sm:col-span-2"
                    />
                    <Field
                      label="API Key"
                      value={form.ollama_api_key}
                      onChange={set("ollama_api_key")}
                      placeholder="ollama (local) or ollama.com key"
                      secret
                      className="sm:col-span-2"
                    />
                    <p className="text-xs text-muted-foreground sm:col-span-2">
                      Cloud models (e.g. glm-5.2:cloud) require{" "}
                      <code className="rounded bg-muted px-1 py-0.5">ollama signin</code> and{" "}
                      <code className="rounded bg-muted px-1 py-0.5">
                        ollama pull glm-5.2:cloud
                      </code>
                      . Local Ollama uses API key <code className="rounded bg-muted px-1 py-0.5">ollama</code>.
                    </p>
                  </>
                )}
                {provider === "custom" && (
                  <>
                    <Field
                      label="API Base URL"
                      value={form.local_llm_api_base}
                      onChange={set("local_llm_api_base")}
                      placeholder="http://llm.demo.invalid/v1"
                      className="sm:col-span-2"
                    />
                    <Field
                      label="API Key (optional)"
                      value={form.local_llm_api_key}
                      onChange={set("local_llm_api_key")}
                      placeholder="sk-..."
                      secret
                      className="sm:col-span-2"
                    />
                  </>
                )}
              </div>

              <Button type="submit" disabled={saving} className="gap-1.5">
                <Save className="size-4" />
                {saving ? "Saving..." : "Save & Apply"}
              </Button>
            </CardContent>
          </Card>
        </form>
      )}

      {!provider && (
        <div className="flex flex-col items-center rounded-lg border border-dashed bg-muted/30 p-8 text-center">
          <Server className="mb-3 size-8 text-muted-foreground/50" />
          <p className="text-sm text-muted-foreground">
            Select a provider above to configure credentials.
          </p>
        </div>
      )}

      {/* Task Routing Table */}
      <TaskRoutingSection />
    </div>
  );
}

/* ── Task Routing Section ────────────────────────────────────── */

function TaskRoutingSection() {
  const [routes, setRoutes] = useState<TaskRoute[]>([]);
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  const reload = () => {
    setLoading(true);
    getLLMRouting()
      .then((data) => {
        setRoutes(data);
        setEdits({});
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  };

  useEffect(reload, []);

  useEffect(() => {
    if (!msg) return;
    const id = window.setTimeout(() => setMsg(null), 4000);
    return () => window.clearTimeout(id);
  }, [msg]);

  const handleModelEdit = (task: string, value: string) => {
    setEdits((e) => ({ ...e, [task]: value }));
  };

  const handleSave = async () => {
    const overrides = Object.entries(edits)
      .filter(([, v]) => v !== undefined)
      .map(([task, model]) => ({ task, model: model || "" }));
    if (overrides.length === 0) return;
    setSaving(true);
    try {
      await updateLLMRouting(overrides);
      setMsg(`Saved ${overrides.length} override(s)`);
      reload();
    } catch (err) {
      setMsg(err instanceof Error ? err.message : "Save failed");
    } finally {
      setSaving(false);
    }
  };

  const handleReset = async (task: string) => {
    setSaving(true);
    try {
      await updateLLMRouting([{ task, model: "" }]);
      setMsg(`Reset ${task} to default`);
      reload();
    } catch {
      setMsg("Reset failed");
    } finally {
      setSaving(false);
    }
  };

  const hasEdits = Object.keys(edits).length > 0;

  if (loading) return null;

  return (
    <Card className="overflow-hidden border border-border/80 bg-card shadow-sm">
      <CardHeader className="pb-2">
        <CardTitle className="text-xs font-medium uppercase tracking-[0.14em] text-muted-foreground">
          Task Routing — Which Agent Uses Which LLM
        </CardTitle>
      </CardHeader>
      <CardContent>
        {msg && (
          <div className="mb-3 rounded-lg border border-success/30 bg-success/10 px-4 py-2.5 text-sm text-success">
            {msg}
          </div>
        )}
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b">
                <th className="px-3 py-2 text-left text-xs font-medium uppercase tracking-[0.14em] text-muted-foreground">
                  Task
                </th>
                <th className="px-3 py-2 text-left text-xs font-medium uppercase tracking-[0.14em] text-muted-foreground">
                  Current Model
                </th>
                <th className="w-64 px-3 py-2 text-left text-xs font-medium uppercase tracking-[0.14em] text-muted-foreground">
                  Override
                </th>
                <th className="w-16 px-3 py-2" />
              </tr>
            </thead>
            <tbody>
              {routes.map((r) => (
                <tr
                  key={r.task}
                  className="border-b border-border/50 transition-colors hover:bg-muted/30"
                >
                  <td className="px-3 py-2">
                    <div className="font-medium">{r.label}</div>
                    <div className="font-mono text-xs text-muted-foreground">
                      {r.task}
                    </div>
                  </td>
                  <td className="px-3 py-2">
                    <code className="rounded bg-muted px-1.5 py-0.5 text-xs">
                      {r.model}
                    </code>
                    {r.api_base && (
                      <div className="mt-0.5 max-w-48 truncate text-xs text-muted-foreground">
                        {r.api_base}
                      </div>
                    )}
                  </td>
                  <td className="px-3 py-2">
                    <input
                      type="text"
                      value={edits[r.task] ?? ""}
                      onChange={(e) => handleModelEdit(r.task, e.target.value)}
                      placeholder="Use default"
                      className="w-full rounded-lg border border-border bg-background px-2 py-1 font-mono text-xs shadow-sm outline-none transition focus-visible:ring-2 focus-visible:ring-primary/40"
                    />
                  </td>
                  <td className="px-3 py-2">
                    {r.has_override && (
                      <Button
                        size="sm"
                        variant="ghost"
                        className="gap-1 text-xs text-danger hover:bg-danger/10"
                        disabled={saving}
                        onClick={() => handleReset(r.task)}
                      >
                        <RotateCcw className="size-3" />
                        Reset
                      </Button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {hasEdits && (
          <Button
            className="mt-3 gap-1.5"
            disabled={saving}
            onClick={handleSave}
          >
            <Check className="size-4" />
            {saving ? "Saving..." : "Apply Overrides"}
          </Button>
        )}
      </CardContent>
    </Card>
  );
}

/* ── Sub-components ──────────────────────────────────────────── */

function Field({
  label,
  value,
  onChange,
  placeholder,
  secret,
  className,
}: {
  label: string;
  value: string;
  onChange: (e: React.ChangeEvent<HTMLInputElement>) => void;
  placeholder?: string;
  secret?: boolean;
  className?: string;
}) {
  return (
    <label className={cn("flex flex-col gap-1.5", className)}>
      <span className="text-xs font-medium text-muted-foreground">{label}</span>
      <input
        type={secret ? "password" : "text"}
        value={value}
        onChange={onChange}
        placeholder={placeholder}
        autoComplete="off"
        className="rounded-lg border border-border bg-background px-3 py-2 font-mono text-xs shadow-sm outline-none transition focus-visible:ring-2 focus-visible:ring-primary/40"
      />
    </label>
  );
}
