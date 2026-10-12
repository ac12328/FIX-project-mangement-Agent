"use client";

import { useCallback, useEffect, useState } from "react";
import {
  Brain,
  ChevronDown,
  ChevronRight,
  Layers,
  MessageSquare,
  RefreshCw,
  Search,
  Send,
  Trash2,
  ArrowUpRight,
  ArrowDownLeft,
  AlertTriangle,
} from "lucide-react";

import { EmptyState } from "@/components/empty-state";
import { PageHeader } from "@/components/page-header";
import { ErrorPanel } from "@/components/state-panel";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import {
  getConversationContext,
  clearConversationSummary,
  previewAgentContext,
} from "@/lib/api";
import type { ConversationContext, ContextPreviewResponse, ContextLayerResponse } from "@/lib/types";
import { notifyError, notifySuccess } from "@/lib/notifications";
import { cn } from "@/lib/utils";

export default function ConversationContextPage() {
  const [data, setData] = useState<ConversationContext | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [clearing, setClearing] = useState(false);

  // Preview state
  const [previewMessage, setPreviewMessage] = useState("");
  const [previewResult, setPreviewResult] = useState<ContextPreviewResponse | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [expandedLayers, setExpandedLayers] = useState<Set<string>>(new Set());

  const load = useCallback(async () => {
    try {
      setLoading(true);
      const ctx = await getConversationContext();
      setData(ctx);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load conversation context");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const handleClear = async () => {
    if (!confirm("Clear the conversation summary? The agent will start fresh on the next message.")) return;
    try {
      setClearing(true);
      await clearConversationSummary();
      notifySuccess("Conversation summary cleared", "The next agent message will start with a fresh conversation summary.");
      await load();
    } catch (e) {
      const message = e instanceof Error ? e.message : "Failed to clear summary";
      setError(message);
      notifyError(e, message);
    } finally {
      setClearing(false);
    }
  };

  const handlePreview = async () => {
    if (!previewMessage.trim()) return;
    try {
      setPreviewLoading(true);
      setPreviewError(null);
      const result = await previewAgentContext(previewMessage.trim());
      setPreviewResult(result);
      setExpandedLayers(new Set());
    } catch (e) {
      setPreviewError(e instanceof Error ? e.message : "Failed to preview context");
    } finally {
      setPreviewLoading(false);
    }
  };

  const toggleLayer = (name: string) => {
    setExpandedLayers((prev) => {
      const next = new Set(prev);
      if (next.has(name)) {
        next.delete(name);
      } else {
        next.add(name);
      }
      return next;
    });
  };

  const expandAll = () => {
    if (previewResult) {
      setExpandedLayers(new Set(previewResult.layers.map((l) => l.name)));
    }
  };

  const collapseAll = () => {
    setExpandedLayers(new Set());
  };

  return (
    <div className="mx-auto max-w-[1600px] space-y-6">
      <PageHeader
        eyebrow="AI & Analytics"
        title="Agent context"
        description="Preview the exact context layers assembled for a message, or inspect persistent conversation state."
        metadata={data ? `${data.recent_messages.length} conversation messages` : undefined}
        actions={
          <Button variant="outline" disabled={loading} onClick={() => void load()}>
            <RefreshCw className="size-4" />
            Refresh
          </Button>
        }
      />

      {error ? <ErrorPanel description={error} onRetry={() => void load()} /> : null}

      <Tabs defaultValue="preview">
        <TabsList>
          <TabsTrigger value="preview">
            <Search className="size-3.5" />
            Context Preview
          </TabsTrigger>
          <TabsTrigger value="state">
            <Brain className="size-3.5" />
            Persistent State
          </TabsTrigger>
        </TabsList>

        {/* ── Tab 1: Context Preview ─────────────────────────────────── */}
        <TabsContent value="preview" className="mt-4 space-y-4">
          {/* Input area */}
          <Card className="border border-border/80 bg-card shadow-sm">
            <CardContent className="pt-4">
              <div className="flex gap-3">
                <Textarea
                  placeholder="Type a simulated message to preview what context the agent would receive..."
                  value={previewMessage}
                  onChange={(e) => setPreviewMessage(e.target.value)}
                  className="min-h-[60px] resize-none text-sm"
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                      handlePreview();
                    }
                  }}
                />
                <Button
                  onClick={handlePreview}
                  disabled={previewLoading || !previewMessage.trim()}
                  className="shrink-0 gap-1.5"
                >
                  <Send className="size-3.5" />
                  {previewLoading ? "Loading..." : "Preview"}
                </Button>
              </div>
              <p className="mt-1.5 text-xs text-muted-foreground">
                Press Ctrl+Enter to submit. This simulates the context assembly without actually sending the message.
              </p>
            </CardContent>
          </Card>

          {previewError && (
            <div className="rounded-lg border border-danger/30 bg-danger/10 px-4 py-2.5 text-sm text-danger">
              {previewError}
            </div>
          )}

          {previewLoading && <PreviewSkeleton />}

          {/* Preview results */}
          {previewResult && !previewLoading && (
            <div className="space-y-4">
              {/* Metadata bar */}
              <Card className="border border-border/80 bg-card shadow-sm">
                <CardContent className="pt-4">
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge variant="outline" className="gap-1">
                      Intent: <span className="font-bold">{previewResult.intent}</span>
                    </Badge>
                    {previewResult.detected_project_id && (
                      <Badge variant="outline" className="gap-1">
                        Project: <span className="font-mono font-bold">{previewResult.detected_project_id}</span>
                      </Badge>
                    )}
                    <Badge variant="outline" className="gap-1">
                      Confidence: <span className="font-bold">{(previewResult.context_confidence * 100).toFixed(0)}%</span>
                    </Badge>
                    <Badge variant="outline" className="gap-1">
                      ~{previewResult.total_token_estimate.toLocaleString()} tokens
                    </Badge>
                    {previewResult.planning_hint && (
                      <Badge variant="secondary" className="gap-1">
                        Planning hint active
                      </Badge>
                    )}
                    {previewResult.missing_flags.map((flag) => (
                      <Badge key={flag} variant="destructive" className="gap-1">
                        <AlertTriangle className="size-3" />
                        {flag}
                      </Badge>
                    ))}
                  </div>
                </CardContent>
              </Card>

              {/* Tools available */}
              <Card className="border border-border/80 bg-card shadow-sm">
                <CardHeader className="pb-2">
                  <CardTitle className="text-xs font-medium uppercase tracking-[0.14em] text-muted-foreground">
                    Available Tools ({previewResult.available_tools.length})
                  </CardTitle>
                </CardHeader>
                <CardContent>
                  <div className="flex flex-wrap gap-1.5">
                    {previewResult.available_tools.map((tool) => (
                      <span
                        key={tool}
                        className="rounded border border-border bg-muted/50 px-2 py-0.5 font-mono text-xs"
                      >
                        {tool}
                      </span>
                    ))}
                  </div>
                </CardContent>
              </Card>

              {/* Context layers accordion */}
              <Card className="border border-border/80 bg-card shadow-sm">
                <CardHeader className="pb-2">
                  <div className="flex items-center justify-between">
                    <CardTitle className="flex items-center gap-2 text-xs font-medium uppercase tracking-[0.14em] text-muted-foreground">
                      <Layers className="size-3.5" />
                      Context Layers
                    </CardTitle>
                    <div className="flex gap-1">
                      <Button size="xs" variant="ghost" onClick={expandAll}>
                        Expand all
                      </Button>
                      <Button size="xs" variant="ghost" onClick={collapseAll}>
                        Collapse all
                      </Button>
                    </div>
                  </div>
                </CardHeader>
                <CardContent className="space-y-1">
                  {previewResult.layers.map((layer) => (
                    <LayerAccordion
                      key={layer.name}
                      layer={layer}
                      expanded={expandedLayers.has(layer.name)}
                      onToggle={() => toggleLayer(layer.name)}
                    />
                  ))}
                </CardContent>
              </Card>
            </div>
          )}

          {!previewResult && !previewLoading && (
            <EmptyState
              icon={Search}
              title="No preview yet"
              description="Type a message above and click Preview to see what context the agent would assemble."
              className="p-10"
            />
          )}
        </TabsContent>

        {/* ── Tab 2: Persistent State ────────────────────────────────── */}
        <TabsContent value="state" className="mt-4">
          <div className="grid gap-4 xl:grid-cols-2">
            {/* Progress/Summary */}
            <Card className="border border-border/80 bg-card shadow-sm">
              <CardHeader className="pb-2">
                <div className="flex items-center justify-between">
                  <CardTitle className="flex items-center gap-2 text-xs font-medium uppercase tracking-[0.14em] text-muted-foreground">
                    <Brain className="size-3.5" />
                    {data?.progress ? "Agent Progress" : "Persistent Summary (last 20 turns)"}
                  </CardTitle>
                  {data?.summary && (
                    <Button
                      size="sm"
                      variant="outline"
                      className="h-7 gap-1 text-xs text-danger hover:bg-danger/10 hover:text-danger"
                      onClick={handleClear}
                      disabled={clearing}
                    >
                      <Trash2 className="size-3" />
                      {clearing ? "Clearing..." : "Clear"}
                    </Button>
                  )}
                </div>
              </CardHeader>
              <CardContent>
                {loading ? (
                  <SummarySkeleton />
                ) : data?.progress ? (
                  <div className="space-y-4">
                    {Object.keys(data.progress.projects).length > 0 && (
                      <div>
                        <h4 className="text-xs font-semibold text-muted-foreground mb-2">Active Projects</h4>
                        <div className="space-y-2">
                          {Object.entries(data.progress.projects).map(([pid, info]) => (
                            <div key={pid} className="rounded-md border p-2 text-sm">
                              <div className="flex items-center gap-2 mb-1">
                                <span className="font-mono font-bold text-primary">{pid}</span>
                                <span className="text-xs bg-muted px-1.5 py-0.5 rounded">{info.state}</span>
                              </div>
                              <p className="text-muted-foreground text-xs">{info.last_action}</p>
                            </div>
                          ))}
                        </div>
                      </div>
                    )}
                    {data.progress.recent_decisions.length > 0 && (
                      <div>
                        <h4 className="text-xs font-semibold text-muted-foreground mb-2">Recent Decisions</h4>
                        <ul className="text-sm space-y-1">
                          {data.progress.recent_decisions.map((d, i) => <li key={i} className="text-xs">{d}</li>)}
                        </ul>
                      </div>
                    )}
                    {data.progress.pending.length > 0 && (
                      <div>
                        <h4 className="text-xs font-semibold text-muted-foreground mb-2">Pending</h4>
                        <ul className="text-sm space-y-1">
                          {data.progress.pending.map((p, i) => <li key={i} className="text-xs">{p}</li>)}
                        </ul>
                      </div>
                    )}
                    {data.progress.session_notes && (
                      <div>
                        <h4 className="text-xs font-semibold text-muted-foreground mb-2">Session Notes</h4>
                        <p className="text-xs italic text-muted-foreground">{data.progress.session_notes}</p>
                      </div>
                    )}
                    {data.progress.updated_at && (
                      <p className="text-xs text-muted-foreground mt-2">Last updated: {new Date(data.progress.updated_at).toLocaleString()}</p>
                    )}
                  </div>
                ) : data?.summary ? (
                  <div>
                    <pre className="whitespace-pre-wrap rounded-lg bg-muted/30 p-4 font-mono text-xs leading-relaxed text-foreground/90">
                      {data.summary}
                    </pre>
                    {data.summary_updated_at && (
                      <p className="mt-2 text-xs text-muted-foreground">
                        Last updated: {new Date(data.summary_updated_at).toLocaleString()}
                      </p>
                    )}
                  </div>
                ) : (
                  <p className="text-sm text-muted-foreground">No progress data yet. Progress will appear after the agent handles messages.</p>
                )}
              </CardContent>
            </Card>

            {/* Recent Messages */}
            <Card className="border border-border/80 bg-card shadow-sm">
              <CardHeader className="pb-2">
                <CardTitle className="flex items-center gap-2 text-xs font-medium uppercase tracking-[0.14em] text-muted-foreground">
                  <MessageSquare className="size-3.5" />
                  Recent Messages (last 10)
                </CardTitle>
              </CardHeader>
              <CardContent>
                {loading ? (
                  <MessagesSkeleton />
                ) : !data?.recent_messages.length ? (
                  <EmptyState
                    icon={MessageSquare}
                    title="No messages yet"
                    description="Recent Telegram messages will appear here once you start chatting."
                    className="p-5"
                  />
                ) : (
                  <div className="space-y-2">
                    {data.recent_messages.map((msg, i) => (
                      <div
                        key={i}
                        className={cn(
                          "rounded-lg border border-border/50 p-3",
                          msg.direction === "inbound" ? "bg-primary/5" : "bg-muted/20",
                        )}
                      >
                        <div className="flex items-center gap-2">
                          {msg.direction === "inbound" ? (
                            <span className="flex items-center gap-1 rounded-full border border-primary/30 bg-primary/10 px-2 py-0.5 text-xs font-medium text-primary">
                              <ArrowDownLeft className="size-2.5" />
                              You
                            </span>
                          ) : (
                            <span className="flex items-center gap-1 rounded-full border border-success/30 bg-success/10 px-2 py-0.5 text-xs font-medium text-success">
                              <ArrowUpRight className="size-2.5" />
                              Agent
                            </span>
                          )}
                          <span className="rounded-full border border-border bg-muted px-1.5 py-0.5 text-[9px] text-muted-foreground">
                            {msg.message_kind}
                          </span>
                          <span className="ml-auto text-xs text-muted-foreground">
                            {new Date(msg.created_at).toLocaleTimeString([], {
                              hour: "2-digit",
                              minute: "2-digit",
                            })}
                          </span>
                        </div>
                        <p className="mt-1.5 text-xs leading-relaxed text-foreground/80">
                          {msg.text.length > 300 ? msg.text.slice(0, 300) + "..." : msg.text}
                        </p>
                      </div>
                    ))}
                  </div>
                )}
              </CardContent>
            </Card>
          </div>
        </TabsContent>
      </Tabs>
    </div>
  );
}

// ── Sub-components ───────────────────────────────────────────────────

function LayerAccordion({
  layer,
  expanded,
  onToggle,
}: {
  layer: ContextLayerResponse;
  expanded: boolean;
  onToggle: () => void;
}) {
  const hasContent = layer.content.trim().length > 0;

  return (
    <div className="rounded-md border border-border/60">
      <button
        onClick={onToggle}
        disabled={!hasContent}
        className={cn(
          "flex w-full items-center gap-2 px-3 py-2 text-left text-sm transition-colors",
          hasContent
            ? "hover:bg-muted/50 cursor-pointer"
            : "cursor-default opacity-50",
        )}
      >
        {hasContent ? (
          expanded ? (
            <ChevronDown className="size-3.5 shrink-0 text-muted-foreground" />
          ) : (
            <ChevronRight className="size-3.5 shrink-0 text-muted-foreground" />
          )
        ) : (
          <ChevronRight className="size-3.5 shrink-0 text-muted-foreground/30" />
        )}
        <span className="font-medium">{layer.label}</span>
        <span className="ml-auto text-xs text-muted-foreground">
          {hasContent ? `~${layer.token_estimate.toLocaleString()} tokens` : "empty"}
        </span>
      </button>
      {expanded && hasContent && (
        <div className="border-t border-border/40 px-3 py-2">
          <pre className="max-h-[400px] overflow-auto whitespace-pre-wrap rounded bg-muted/30 p-3 font-mono text-xs leading-relaxed text-foreground/85">
            {layer.content}
          </pre>
        </div>
      )}
    </div>
  );
}

function PreviewSkeleton() {
  return (
    <div className="space-y-4">
      <Card className="border border-border/80 bg-card shadow-sm">
        <CardContent className="pt-4">
          <div className="flex gap-2">
            <Skeleton className="h-6 w-24 rounded-full" />
            <Skeleton className="h-6 w-28 rounded-full" />
            <Skeleton className="h-6 w-20 rounded-full" />
          </div>
        </CardContent>
      </Card>
      <Card className="border border-border/80 bg-card shadow-sm">
        <CardContent className="pt-4 space-y-2">
          {Array.from({ length: 5 }).map((_, i) => (
            <Skeleton key={i} className="h-10 w-full rounded-md" />
          ))}
        </CardContent>
      </Card>
    </div>
  );
}

function SummarySkeleton() {
  return (
    <div className="space-y-2">
      <Skeleton className="h-4 w-full" />
      <Skeleton className="h-4 w-5/6" />
      <Skeleton className="h-4 w-4/6" />
      <Skeleton className="h-4 w-full" />
      <Skeleton className="h-4 w-3/6" />
    </div>
  );
}

function MessagesSkeleton() {
  return (
    <div className="space-y-2">
      {Array.from({ length: 4 }).map((_, i) => (
        <div key={i} className="rounded-lg border border-border/50 p-3">
          <div className="flex items-center gap-2">
            <Skeleton className="h-5 w-14 rounded-full" />
            <Skeleton className="h-3 w-16" />
          </div>
          <Skeleton className="mt-2 h-4 w-full" />
        </div>
      ))}
    </div>
  );
}
