"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { Inbox, Sparkles } from "lucide-react";

import { PageHeader } from "@/components/page-header";
import { StatePanel, ErrorPanel, LoadingPanel } from "@/components/state-panel";
import { StatusBadge } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  ApiError,
  backfillInboxInsights,
  getInboxInsights,
  getRuntimeCapabilities,
  type InboxInsight,
} from "@/lib/api";
import { notifyError, notifySuccess } from "@/lib/notifications";

export default function InboxPage() {
  const [items, setItems] = useState<InboxInsight[]>([]);
  const [error, setError] = useState("");
  const [disabled, setDisabled] = useState(false);
  const [loading, setLoading] = useState(true);
  const [analyzing, setAnalyzing] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const capabilities = await getRuntimeCapabilities();
      if (!capabilities.personal_assistant.capabilities.inbox_intelligence) {
        setDisabled(true);
        setItems([]);
        setError("");
        return;
      }
      setDisabled(false);
      setItems(await getInboxInsights());
      setError("");
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        setDisabled(true);
        setItems([]);
        setError("");
        return;
      }
      setError(err instanceof Error ? err.message : "Could not load prioritized inbound.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  async function analyzeRecent() {
    setAnalyzing(true);
    try {
      const result = await backfillInboxInsights(1);
      if (result.errors.length && !result.analyzed) {
        notifyError(new Error(result.errors[0]), "Could not analyze recent inbound.");
        return;
      }
      notifySuccess(
        result.analyzed ? `Analyzed ${result.analyzed} inbound message${result.analyzed === 1 ? "" : "s"}` : "No new inbound to analyze",
        result.has_more || result.remaining_unanalyzed ? "More recent mail can still be scored." : undefined,
      );
      await load();
    } catch (err) {
      notifyError(err, "Could not analyze recent inbound.");
    } finally {
      setAnalyzing(false);
    }
  }

  return (
    <div className="mx-auto max-w-[1400px] space-y-6">
      <PageHeader
        eyebrow="Mail"
        title="Prioritized inbound"
        description="Onboarding, inquiry, and support mail that needs a PM reply. Internal, settlement, and bounce mail stay out."
        metadata={!loading && !disabled && !error ? `${items.length} action${items.length === 1 ? "" : "s"}` : undefined}
        actions={
          disabled ? undefined : (
            <Button variant="outline" onClick={() => void analyzeRecent()} disabled={analyzing || loading}>
              <Sparkles className="size-4" />
              {analyzing ? "Analyzing…" : "Analyze recent inbound"}
            </Button>
          )
        }
      />

      {disabled ? (
        <StatePanel
          icon={Sparkles}
          title="Inbox intelligence is off"
          description="This experiment ranks inbound mail that needs a reply. Turn on inbox_intelligence_enabled to use it."
        />
      ) : null}

      {error ? <ErrorPanel description={error} onRetry={() => void load()} /> : null}
      {loading && !items.length && !disabled ? <LoadingPanel label="Loading prioritized inbound" /> : null}

      {!loading && !error && !disabled && !items.length ? (
        <StatePanel
          icon={Inbox}
          title="No inbound needs a reply"
          description="Only project-linked or FIX/inquiry/support mail is scored. Analyze recent inbound to score candidates already in the mailbox."
          actionLabel={analyzing ? "Analyzing…" : "Analyze recent inbound"}
          onAction={() => { if (!analyzing) void analyzeRecent(); }}
        />
      ) : null}

      {items.length ? (
        <section className="grid gap-4 xl:grid-cols-2" aria-label="Prioritized inbound">
          {items.map((item) => {
            const projectId = item.related_entities?.find((entity) => entity.type === "project")?.id;
            return (
              <Card key={item.insight_id} className="border bg-card shadow-xs">
                <CardHeader className="border-b pb-4">
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="min-w-0">
                      <CardTitle className="text-base leading-6">{item.request_summary ?? item.email_id}</CardTitle>
                      <p className="mt-1 font-mono text-xs text-muted-foreground">{item.email_id}</p>
                    </div>
                    <div className="flex flex-wrap gap-2">
                      <StatusBadge value={priorityValue(item.priority)} label={item.priority} />
                      {item.action_required ? <StatusBadge value="yellow" label="Action required" /> : null}
                    </div>
                  </div>
                </CardHeader>
                <CardContent className="space-y-4 pt-0">
                  {item.priority_reasons.length ? (
                    <ul className="space-y-2 text-sm text-muted-foreground">
                      {item.priority_reasons.map((reason) => <li key={reason}>{reason}</li>)}
                    </ul>
                  ) : null}
                  {item.evidence_spans.length ? (
                    <blockquote className="rounded-lg border-l-4 border-primary/40 bg-muted/40 px-4 py-3 text-sm leading-6">
                      {item.evidence_spans[0]}
                    </blockquote>
                  ) : null}
                  {item.suggested_action ? (
                    <div className="rounded-lg border border-primary/20 bg-primary/5 px-4 py-3">
                      <p className="text-xs font-semibold uppercase tracking-[0.12em] text-primary">Suggested next action</p>
                      <p className="mt-1 text-sm leading-6 text-foreground">{item.suggested_action}</p>
                    </div>
                  ) : null}
                  {projectId ? (
                    <Button variant="outline" size="sm" render={<Link href={`/projects/${encodeURIComponent(projectId)}`} />}>
                      Open {projectId}
                    </Button>
                  ) : null}
                </CardContent>
              </Card>
            );
          })}
        </section>
      ) : null}
    </div>
  );
}

function priorityValue(priority: string): string {
  if (priority === "urgent" || priority === "high") return "red";
  if (priority === "medium" || priority === "normal") return "yellow";
  return "low";
}
