"use client";

import { useCallback, useEffect, useState } from "react";
import { BookOpen, RefreshCw, Eye, EyeOff, ChevronDown, ChevronRight } from "lucide-react";

import { EmptyState } from "@/components/empty-state";
import { PageHeader } from "@/components/page-header";
import { PageToolbar, ToolbarField } from "@/components/page-toolbar";
import { ErrorPanel } from "@/components/state-panel";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";
import {
  getLearningRecent,
  getLearningStats,
  getLearningEntry,
  type KnowledgeEntry,
  type KnowledgeEntryDetail,
  type LearningStats,
} from "@/lib/api";

const TYPE_LABELS: Record<string, string> = {
  domain_fact: "Domain Fact",
  problem_solution: "Problem / Solution",
  process_step: "Process Step",
};

const TYPE_COLORS: Record<string, string> = {
  domain_fact: "bg-info/10 text-info",
  problem_solution: "bg-warning/10 text-warning",
  process_step: "bg-success/10 text-success",
};

const CONFIDENCE_WIDTH: Record<string, string> = {
  low: "w-1/3",
  medium: "w-2/3",
  high: "w-full",
};

type Period = "7" | "30" | "90";

function StatCard({ label, value }: { label: string; value: string | number }) {
  return (
    <Card>
      <CardContent className="p-4">
        <p className="text-xs text-muted-foreground">{label}</p>
        <p className="text-2xl font-bold">{value}</p>
      </CardContent>
    </Card>
  );
}

function EntryCard({ entry }: { entry: KnowledgeEntry }) {
  const [expanded, setExpanded] = useState(false);
  const [detail, setDetail] = useState<KnowledgeEntryDetail | null>(null);

  const expand = async () => {
    if (expanded) {
      setExpanded(false);
      return;
    }
    try {
      const d = await getLearningEntry(entry.entry_id);
      setDetail(d);
    } catch { /* ignore */ }
    setExpanded(true);
  };

  return (
    <Card className="transition-shadow hover:shadow-sm">
      <CardContent className="p-4 space-y-2">
        <div className="flex items-start justify-between gap-2">
          <div className="flex items-center gap-2 flex-wrap">
            <Badge variant="secondary" className={cn("text-xs", TYPE_COLORS[entry.knowledge_type])}>
              {TYPE_LABELS[entry.knowledge_type] ?? entry.knowledge_type}
            </Badge>
            {entry.visibility === "internal_only" && (
              <Badge variant="outline" className="text-xs gap-1">
                <EyeOff className="size-3" /> Internal
              </Badge>
            )}
            {entry.visibility === "public" && (
              <Badge variant="outline" className="text-xs gap-1 text-success">
                <Eye className="size-3" /> Public
              </Badge>
            )}
          </div>
          <span className="text-xs text-muted-foreground whitespace-nowrap">
            {entry.created_at?.slice(0, 10)}
          </span>
        </div>

        <p className="text-sm font-semibold">{entry.title}</p>
        <p className="text-sm text-muted-foreground">{entry.content}</p>

        <div className="flex items-center gap-3 text-xs text-muted-foreground">
          {entry.project_name && (
            <span>Project: <span className="font-medium text-foreground">{entry.project_name}</span></span>
          )}
          <span>Confidence:</span>
          <div className="w-16 h-1.5 rounded-full bg-muted overflow-hidden">
            <div className={cn("h-full rounded-full bg-primary", CONFIDENCE_WIDTH[entry.confidence])} />
          </div>
          <span>{entry.confidence}</span>
        </div>

        <button
          onClick={expand}
          className="flex min-h-11 items-center gap-1 text-xs text-muted-foreground transition-colors hover:text-foreground"
        >
          {expanded ? <ChevronDown className="size-3" /> : <ChevronRight className="size-3" />}
          {entry.source_email_count} source email{entry.source_email_count !== 1 ? "s" : ""}
        </button>

        {expanded && detail && detail.source_emails.length > 0 && (
          <div className="mt-2 pl-4 border-l-2 space-y-1">
            {detail.source_emails.map((se) => (
              <div key={se.email_id} className="text-xs text-muted-foreground">
                <span className={cn("font-medium", se.direction === "inbound" ? "text-info" : "text-success")}>
                  [{se.direction}]
                </span>{" "}
                {se.sender_email} — {se.subject}
              </div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

export default function LearningPage() {
  const [entries, setEntries] = useState<KnowledgeEntry[]>([]);
  const [stats, setStats] = useState<LearningStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [period, setPeriod] = useState<Period>("7");
  const [typeFilter, setTypeFilter] = useState<string>("all");
  const [visFilter, setVisFilter] = useState<string>("all");

  const load = useCallback(async () => {
    try {
      setLoading(true);
      const params: Record<string, string | number> = { days: Number(period) };
      if (typeFilter !== "all") params.knowledge_type = typeFilter;
      if (visFilter !== "all") params.visibility = visFilter;

      const [e, s] = await Promise.all([
        getLearningRecent(params as Parameters<typeof getLearningRecent>[0]),
        getLearningStats(),
      ]);
      setEntries(e);
      setStats(s);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load");
    } finally {
      setLoading(false);
    }
  }, [period, typeFilter, visFilter]);

  useEffect(() => { load(); }, [load]);

  // Group entries by date
  const grouped = entries.reduce<Record<string, KnowledgeEntry[]>>((acc, entry) => {
    const date = entry.created_at?.slice(0, 10) ?? "Unknown";
    (acc[date] ??= []).push(entry);
    return acc;
  }, {});

  return (
    <div className="mx-auto max-w-[1600px] space-y-6">
      <PageHeader
        eyebrow="AI & Analytics"
        title="Learning"
        description="Inspect attributable knowledge entries mined from operational email threads."
        metadata={stats ? <>{stats.total_entries} total entries <span aria-hidden="true">•</span> Daily job at 03:30 HKT</> : undefined}
        actions={
          <Button variant="outline" onClick={load} disabled={loading}>
            <RefreshCw className={cn("size-4", loading && "animate-spin")} />
            Refresh
          </Button>
        }
      />

      {/* Stats */}
      {stats && (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <StatCard label="Total Entries" value={stats.total_entries} />
          <StatCard label="Domain Facts" value={stats.by_type.domain_fact ?? 0} />
          <StatCard label="Problem / Solution" value={stats.by_type.problem_solution ?? 0} />
          <StatCard label="Process Steps" value={stats.by_type.process_step ?? 0} />
        </div>
      )}

      {stats?.last_run && (
        <p className="text-xs text-muted-foreground">
          Last run: {stats.last_run.started_at?.slice(0, 16)} — {stats.last_run.entries_created} new entries,{" "}
          {stats.last_run.wiki_pages_written} wiki pages
          {" · "}Scheduled daily at 3:30 AM HKT
        </p>
      )}

      {!stats?.last_run && (
        <p className="text-xs text-muted-foreground">
          Scheduled daily at 3:30 AM HKT — no runs recorded yet.
        </p>
      )}

      <PageToolbar summary={`${entries.length} entries in this view`}>
        <ToolbarField label="Knowledge type" className="sm:w-48">
          <Select value={typeFilter} onValueChange={(v) => setTypeFilter(v ?? "all")}>
            <SelectTrigger><SelectValue placeholder="All types" /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All types</SelectItem>
              <SelectItem value="domain_fact">Domain Fact</SelectItem>
              <SelectItem value="problem_solution">Problem / Solution</SelectItem>
              <SelectItem value="process_step">Process Step</SelectItem>
            </SelectContent>
          </Select>
        </ToolbarField>

        <ToolbarField label="Visibility" className="sm:w-44">
          <Select value={visFilter} onValueChange={(v) => setVisFilter(v ?? "all")}>
            <SelectTrigger><SelectValue placeholder="All visibility" /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All visibility</SelectItem>
              <SelectItem value="public">Public</SelectItem>
              <SelectItem value="internal_only">Internal Only</SelectItem>
            </SelectContent>
          </Select>
        </ToolbarField>

        <ToolbarField label="Period" className="sm:w-40">
          <Select value={period} onValueChange={(v) => setPeriod(v as Period)}>
            <SelectTrigger><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="7">Last 7 days</SelectItem>
              <SelectItem value="30">Last 30 days</SelectItem>
              <SelectItem value="90">Last 90 days</SelectItem>
            </SelectContent>
          </Select>
        </ToolbarField>
      </PageToolbar>

      {error ? <ErrorPanel description={error} onRetry={load} /> : null}

      {loading && !error && (
        <div className="space-y-3">
          {[1, 2, 3].map((i) => (
            <Card key={i} className="border border-border/80">
              <CardContent className="p-4 space-y-3">
                <div className="flex items-center gap-2">
                  <Skeleton className="h-5 w-20 rounded-full" />
                  <Skeleton className="h-5 w-16 rounded-full" />
                </div>
                <Skeleton className="h-4 w-3/4" />
                <Skeleton className="h-3 w-full" />
                <Skeleton className="h-3 w-2/3" />
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      {!loading && !error && entries.length === 0 && (
        <EmptyState
          icon={BookOpen}
          title="No learning entries yet"
          description="The email knowledge miner runs daily at 3:30 AM HKT. Entries will appear here once email threads have been processed."
        />
      )}

      {!loading && !error && Object.entries(grouped).map(([date, items]) => (
        <div key={date} className="space-y-2">
          <h3 className="text-xs font-semibold text-muted-foreground uppercase tracking-wider">{date}</h3>
          {items.map((entry) => <EntryCard key={entry.entry_id} entry={entry} />)}
        </div>
      ))}
    </div>
  );
}
