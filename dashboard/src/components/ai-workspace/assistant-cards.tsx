"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import {
  AlertTriangle,
  Check,
  CheckCircle2,
  Database,
  Download,
  ExternalLink,
  FileEdit,
  Loader2,
  ShieldCheck,
  X,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { StatusBadge } from "@/components/status-badge";
import { approveAssistantAction, rejectAssistantAction } from "@/lib/assistant-api";
import { OperatorUpdateCard } from "@/components/ai-workspace/operator-update-card";
import type {
  AssistantActionPart,
  AssistantDraftPart,
  AssistantErrorPart,
  AssistantOperatorUpdatePart,
  AssistantPart,
  AssistantTableColumn,
  AssistantTablePart,
  AssistantToolResultPart,
} from "@/lib/assistant-types";
import { tablePartToCsv } from "@/lib/assistant-workspace";
import { cn } from "@/lib/utils";

interface PartCardsProps {
  parts: AssistantPart[];
  canManageActions?: boolean;
  operatorWrites?: boolean;
  threadId?: string;
  onActivity?: (message: string) => void;
  onResolved?: () => void;
}

function downloadTable(part: AssistantTablePart) {
  const blob = new Blob(["\ufeff", tablePartToCsv(part)], { type: "text/csv;charset=utf-8" });
  const href = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = href;
  anchor.download = part.export_filename || "ai-workspace-results.csv";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(href);
}

const PREVIEW_KEYS = ["project_id", "client_name", "project_status", "ball_with", "next_action"];
const PREVIEW_LIMIT = 3;

function cellText(value: unknown): string {
  if (value === null || value === undefined || value === "") return "";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function humanizeValue(key: string, value: unknown): string {
  const text = cellText(value);
  if (!text) return "";
  if (key === "current_milestone") {
    return text.replaceAll("_", " ").replace(/^M(\d+)\s/, "M$1 · ");
  }
  return text;
}

function previewColumns(part: AssistantTablePart) {
  const preferred = PREVIEW_KEYS
    .map((key) => part.columns.find((column) => column.key === key))
    .filter((column): column is AssistantTableColumn => Boolean(column));
  return preferred.length ? preferred : part.columns.slice(0, 4);
}

function TableCellValue({ columnKey, value }: { columnKey: string; value: unknown }) {
  const text = humanizeValue(columnKey, value);
  if (!text) return <span className="text-muted-foreground">—</span>;
  if (columnKey === "project_id") {
    return (
      <Link href={`/projects/${encodeURIComponent(text)}`} className="font-medium text-primary hover:underline">
        {text}
      </Link>
    );
  }
  if (columnKey === "project_status" || columnKey === "ball_with") {
    return <StatusBadge value={text} />;
  }
  return <span className="break-words">{text}</span>;
}

function ResultsTable({ part, compact = false }: { part: AssistantTablePart; compact?: boolean }) {
  const columns = compact ? previewColumns(part) : part.columns;
  const rows = compact ? part.rows.slice(0, PREVIEW_LIMIT) : part.rows;
  return (
    <div className="max-w-full overflow-x-auto" data-slot="table-container">
      <table className={cn("w-full text-left text-sm", compact ? "" : "min-w-[720px]")}>
        <thead className="bg-muted/70 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          <tr>
            {columns.map((column) => (
              <th key={column.key} className="whitespace-nowrap px-4 py-3">{column.label}</th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          {rows.map((row, index) => (
            <tr key={`${index}-${String(row.project_id ?? "row")}`} className="align-top hover:bg-muted/35">
              {columns.map((column) => (
                <td key={column.key} className={cn("px-4 py-3 text-foreground", compact ? "max-w-48" : "max-w-80")}>
                  <TableCellValue columnKey={column.key} value={row[column.key]} />
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function TableCard({ part }: { part: AssistantTablePart }) {
  const [open, setOpen] = useState(false);
  const remaining = Math.max(0, part.rows.length - PREVIEW_LIMIT);

  return (
    <>
      <Card className="gap-0 py-0 shadow-none">
        <CardHeader className="flex min-h-14 flex-row items-center justify-between gap-3 border-b py-3">
          <div>
            <CardTitle>{part.title || "Results"}</CardTitle>
            <p className="mt-0.5 text-xs text-muted-foreground">{part.rows.length} rows</p>
          </div>
          <Button type="button" size="sm" onClick={() => setOpen(true)}>
            <ExternalLink />
            Open results
          </Button>
        </CardHeader>
        <CardContent className="p-0">
          <ResultsTable part={part} compact />
          {remaining > 0 ? (
            <p className="border-t px-4 py-2 text-xs text-muted-foreground">
              {remaining} more row{remaining === 1 ? "" : "s"}. Open the full table to review every column.
            </p>
          ) : null}
        </CardContent>
      </Card>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="flex max-h-[88dvh] w-full max-w-[min(96vw,80rem)] flex-col overflow-hidden p-0">
          <DialogHeader className="border-b px-6 py-4 pr-14">
            <DialogTitle>{part.title || "Results"}</DialogTitle>
            <DialogDescription>{part.rows.length} rows · {part.columns.length} columns</DialogDescription>
          </DialogHeader>
          <div className="min-h-0 flex-1 overflow-auto">
            <ResultsTable part={part} />
          </div>
          <div className="flex justify-end gap-2 border-t px-6 py-3">
            <Button type="button" variant="outline" onClick={() => downloadTable(part)}>
              <Download />
              Export CSV
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}

function DraftCard({ part }: { part: AssistantDraftPart }) {
  return (
    <Card className="border-primary/20 bg-primary/[0.035] shadow-none">
      <CardHeader className="border-b">
        <div className="flex items-start gap-3">
          <span className="flex size-10 shrink-0 items-center justify-center rounded-xl bg-primary/10 text-primary">
            <FileEdit className="size-5" />
          </span>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <CardTitle>Email draft</CardTitle>
              <Badge variant="secondary">Not sent</Badge>
            </div>
            <p className="mt-1 truncate text-xs text-muted-foreground">
              {part.to_email ? `To ${part.to_email}` : "Recipient not set"}
            </p>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        <div>
          <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Subject</p>
          <p className="mt-1 font-medium">{part.subject}</p>
        </div>
        <div>
          <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Preview</p>
          <p className="mt-1 line-clamp-6 whitespace-pre-wrap text-sm leading-6 text-muted-foreground">{part.body}</p>
        </div>
      </CardContent>
      <CardFooter className="justify-end">
        <Button render={<Link href={part.url} />}>
          Open in Draft Workspace
          <ExternalLink />
        </Button>
      </CardFooter>
    </Card>
  );
}

function ActionCard({
  part,
  canManageActions,
  onActivity,
}: {
  part: AssistantActionPart;
  canManageActions: boolean;
  onActivity?: (message: string) => void;
}) {
  const [status, setStatus] = useState(part.status);
  const [pending, setPending] = useState<"approve" | "reject" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const resolved = !["pending", "proposed"].includes(status.toLowerCase());

  async function decide(decision: "approve" | "reject") {
    setPending(decision);
    setError(null);
    try {
      const result = decision === "approve"
        ? await approveAssistantAction(part.action_id)
        : await rejectAssistantAction(part.action_id);
      setStatus(result.status);
      onActivity?.(`Action ${part.action_id} ${result.status}.`);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "The action could not be updated.");
    } finally {
      setPending(null);
    }
  }

  return (
    <Card className="border-warning/30 bg-warning/[0.045] shadow-none">
      <CardHeader className="border-b">
        <div className="flex items-start gap-3">
          <span className="flex size-10 shrink-0 items-center justify-center rounded-xl bg-warning/15 text-warning">
            <ShieldCheck className="size-5" />
          </span>
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <CardTitle>Approval required</CardTitle>
              <Badge variant="outline" className="capitalize">{part.risk} risk</Badge>
              <Badge variant={resolved ? "secondary" : "outline"} className="capitalize">{status}</Badge>
            </div>
            <p className="mt-1 text-xs text-muted-foreground">{part.action_type} · {part.target_id}</p>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-2">
        <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Proposed changes</p>
        {part.diff.map((change) => (
          <div key={change.field} className="grid gap-2 rounded-lg border bg-background/80 p-3 sm:grid-cols-[8rem_1fr]">
            <span className="font-mono text-xs font-semibold text-muted-foreground">{change.field}</span>
            <div className="flex min-w-0 flex-wrap items-center gap-2 text-sm">
              <span className="break-all text-muted-foreground line-through">{String(change.before ?? "Not set")}</span>
              <span aria-hidden="true">→</span>
              <span className="break-all font-medium">{String(change.after ?? "Not set")}</span>
            </div>
          </div>
        ))}
        {part.expires_at ? <p className="text-xs text-muted-foreground">Expires {new Date(part.expires_at).toLocaleString()}</p> : null}
        {error ? <p role="alert" className="flex items-center gap-2 text-sm text-destructive"><AlertTriangle className="size-4" />{error}</p> : null}
        {!canManageActions && !resolved ? (
          <p className="text-sm text-muted-foreground">Your role can review this proposal but cannot approve business changes.</p>
        ) : null}
      </CardContent>
      {canManageActions && !resolved ? (
        <CardFooter className="justify-end gap-2">
          <Button type="button" variant="outline" onClick={() => void decide("reject")} disabled={pending !== null}>
            {pending === "reject" ? <Loader2 className="animate-spin" /> : <X />}
            Reject
          </Button>
          <Button type="button" onClick={() => void decide("approve")} disabled={pending !== null}>
            {pending === "approve" ? <Loader2 className="animate-spin" /> : <Check />}
            Approve and apply
          </Button>
        </CardFooter>
      ) : null}
      {resolved ? (
        <CardFooter className="gap-2 text-sm font-medium text-success">
          <CheckCircle2 className="size-4" />
          Action {status}
        </CardFooter>
      ) : null}
    </Card>
  );
}

function ToolResultCard({ part }: { part: AssistantToolResultPart }) {
  return (
    <div className="flex items-start gap-2 border-l-2 border-border pl-2.5 text-xs leading-5 text-muted-foreground" data-testid="assistant-tool-result">
      <Database className="mt-0.5 size-3.5 shrink-0" />
      <p className="min-w-0 break-words">
        <span>{part.text || part.tool.replaceAll("_", " ")}</span>
        {part.status ? <span className="ml-1.5 text-[11px] opacity-75">· {part.status}</span> : null}
      </p>
    </div>
  );
}

function ErrorCard({ part }: { part: AssistantErrorPart }) {
  return (
    <div role="alert" className="flex items-start gap-3 rounded-xl border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive">
      <AlertTriangle className="mt-0.5 size-4 shrink-0" />
      <div><p className="font-medium">Request failed</p><p className="mt-1 leading-5">{part.message}</p></div>
    </div>
  );
}

export function AssistantPartCards({
  parts,
  canManageActions = false,
  operatorWrites = false,
  threadId,
  onActivity,
  onResolved,
}: PartCardsProps) {
  const visible = useMemo(() => parts.filter((part) => !["text", "source", "follow_up_questions"].includes(part.type)), [parts]);
  if (visible.length === 0) return null;
  return (
    <div className="mt-3 space-y-3">
      {visible.map((part, index) => {
        const key = `${part.type}-${index}`;
        if (part.type === "table") return <TableCard key={key} part={part as AssistantTablePart} />;
        if (part.type === "draft") return <DraftCard key={key} part={part as AssistantDraftPart} />;
        if (part.type === "action") return <ActionCard key={key} part={part as AssistantActionPart} canManageActions={canManageActions} onActivity={onActivity} />;
        if (part.type === "operator_update") {
          return (
            <OperatorUpdateCard
              key={key}
              part={part as AssistantOperatorUpdatePart}
              threadId={threadId}
              canManageActions={canManageActions && operatorWrites}
              onActivity={onActivity}
              onResolved={onResolved}
            />
          );
        }
        if (part.type === "tool_result") return <ToolResultCard key={key} part={part as AssistantToolResultPart} />;
        if (part.type === "error") return <ErrorCard key={key} part={part as AssistantErrorPart} />;
        return null;
      })}
    </div>
  );
}

export function StatusDot({ status }: { status: "idle" | "running" | "completed" | "failed" }) {
  return <span className={cn("size-2 rounded-full", status === "running" ? "animate-pulse bg-primary" : status === "failed" ? "bg-destructive" : "bg-success")} />;
}
