"use client";

import { useId } from "react";
import { AlertCircle, AlertTriangle, CheckCircle2, RefreshCw, ShieldCheck } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { PreflightIssue, PreflightResult } from "@/lib/draft-workspace-api";

interface PreflightPanelProps {
  preflight: PreflightResult;
  stale?: boolean;
  refreshing?: boolean;
  onRefresh?: () => void;
  onFocusField?: (field: string) => void;
}

function IssueList({
  issues,
  type,
  onFocusField,
}: {
  issues: PreflightIssue[];
  type: "blocker" | "warning";
  onFocusField?: (field: string) => void;
}) {
  const headingId = useId();
  if (!issues.length) return null;
  const Icon = type === "blocker" ? AlertCircle : AlertTriangle;

  return (
    <section aria-labelledby={headingId}>
      <h3
        id={headingId}
        className="mb-2 text-xs font-semibold uppercase tracking-[0.12em] text-muted-foreground"
      >
        {type === "blocker" ? "Must fix" : "Requires confirmation"}
      </h3>
      <ul className="space-y-2">
        {issues.map((issue, index) => (
          <li
            key={`${issue.code}:${issue.field}:${index}`}
            className={
              type === "blocker"
                ? "flex gap-2.5 rounded-lg border border-danger/30 bg-danger/8 p-3 text-danger"
                : "flex gap-2.5 rounded-lg border border-warning/35 bg-warning/8 p-3 text-foreground"
            }
          >
            <Icon
              className={
                type === "blocker"
                  ? "mt-0.5 size-4 shrink-0"
                  : "mt-0.5 size-4 shrink-0 text-warning"
              }
            />
            <div className="min-w-0 flex-1">
              <p className="text-sm leading-5">{issue.message}</p>
              {issue.field && onFocusField && (
                <button
                  type="button"
                  className="mt-1 text-xs font-medium underline underline-offset-2"
                  onClick={() => onFocusField(issue.field)}
                >
                  Go to {issue.field.replaceAll("_", " ")}
                </button>
              )}
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}

export function PreflightPanel({
  preflight,
  stale = false,
  refreshing = false,
  onRefresh,
  onFocusField,
}: PreflightPanelProps) {
  const clean = !preflight.blockers.length && !preflight.warnings.length;

  return (
    <Card size="sm" id="preflight" className="border border-border/80 shadow-sm">
      <CardHeader className="border-b">
        <CardTitle className="flex items-center justify-between gap-3">
          <span className="flex items-center gap-2">
            <ShieldCheck className="size-4 text-primary" /> Send preflight
          </span>
          {onRefresh && (
            <Button
              type="button"
              size="icon-xs"
              variant="ghost"
              aria-label="Refresh preflight"
              title="Refresh preflight"
              disabled={refreshing || stale}
              onClick={onRefresh}
            >
              <RefreshCw className={refreshing ? "size-4 animate-spin" : "size-4"} />
            </Button>
          )}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4" aria-live="polite">
        {stale && (
          <div className="flex gap-2 rounded-lg border border-info/30 bg-info/8 p-3 text-sm text-foreground">
            <RefreshCw className="mt-0.5 size-4 shrink-0 text-info" />
            Save the working copy to refresh server-validated results.
          </div>
        )}
        {!stale && clean && (
          <div className="flex gap-2 rounded-lg border border-success/30 bg-success/8 p-3 text-sm text-foreground">
            <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-success" />
            Mandatory checks passed. Final preview is ready.
          </div>
        )}
        <IssueList
          issues={preflight.blockers}
          type="blocker"
          onFocusField={onFocusField}
        />
        <IssueList
          issues={preflight.warnings}
          type="warning"
          onFocusField={onFocusField}
        />
        <p className="text-xs text-muted-foreground">
          Validated against server revision {preflight.revision}. Warnings must be confirmed
          again in final preview.
        </p>
      </CardContent>
    </Card>
  );
}
