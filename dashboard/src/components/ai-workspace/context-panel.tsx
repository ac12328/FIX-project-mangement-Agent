"use client";

import { useMemo } from "react";
import Link from "next/link";
import {
  Activity,
  BriefcaseBusiness,
  Database,
  ExternalLink,
  FileCheck2,
  FolderKanban,
  Globe2,
  Mail,
  PanelRightClose,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import type {
  AssistantMessage,
  AssistantPart,
  AssistantThread,
} from "@/lib/assistant-types";
import {
  assistantSourceHref,
  isExternalAssistantSource,
} from "@/lib/assistant-sources";
import {
  selectAssistantSources,
  selectPendingAssistantActions,
} from "@/lib/assistant-workspace-parts";
import type { ProjectSummary } from "@/lib/types";
import { cn } from "@/lib/utils";

export type ContextPanelTab = "context" | "sources" | "activity" | "actions";

const TABS = [
  { id: "context", label: "Context", icon: FolderKanban },
  { id: "sources", label: "Sources", icon: Globe2 },
  { id: "activity", label: "Activity", icon: Activity },
  { id: "actions", label: "Pending", icon: FileCheck2 },
] as const;

interface ContextPanelProps {
  thread: AssistantThread;
  projects: ProjectSummary[];
  messages: AssistantMessage[];
  liveParts?: AssistantPart[];
  activity: string[];
  tab: ContextPanelTab;
  sourceIds?: string[] | null;
  onTabChange: (tab: ContextPanelTab) => void;
  onEditProjects: () => void;
  onPortfolio: () => void;
  onClose?: () => void;
}

export function ContextPanel({
  thread,
  projects,
  messages,
  liveParts = [],
  activity,
  tab,
  sourceIds = null,
  onTabChange,
  onEditProjects,
  onPortfolio,
  onClose,
}: ContextPanelProps) {
  const selectedProjects = useMemo(() => {
    const byId = new Map(projects.map((project) => [project.project_id, project]));
    return thread.project_ids.map((projectId) => byId.get(projectId)).filter(Boolean) as ProjectSummary[];
  }, [projects, thread.project_ids]);
  const allSources = useMemo(
    () => selectAssistantSources(messages, liveParts),
    [liveParts, messages],
  );
  const sources = useMemo(() => {
    if (sourceIds === null) return allSources;
    const selected = new Set(sourceIds);
    return allSources.filter((source) => selected.has(source.source_id));
  }, [allSources, sourceIds]);
  const actions = useMemo(
    () => selectPendingAssistantActions(messages, liveParts),
    [liveParts, messages],
  );

  return (
    <aside className="flex h-full min-h-0 flex-col bg-card">
      <div className="flex min-h-14 items-center justify-between border-b px-3">
        <div>
          <p className="font-heading text-sm font-semibold">Workspace details</p>
          <p className="text-[11px] text-muted-foreground">Scope, evidence, and approvals</p>
        </div>
        {onClose ? <Button type="button" size="icon-sm" variant="ghost" aria-label="Close details panel" onClick={onClose}><PanelRightClose /></Button> : null}
      </div>
      <div className="grid grid-cols-4 border-b p-1" role="tablist" aria-label="Workspace details">
        {TABS.map((item) => {
          const Icon = item.icon;
          return (
            <button
              key={item.id}
              type="button"
              role="tab"
              aria-selected={tab === item.id}
              onClick={() => onTabChange(item.id)}
              className={cn(
                "flex min-h-11 flex-col items-center justify-center gap-1 rounded-lg px-1 text-[10px] font-semibold transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                tab === item.id ? "bg-accent text-accent-foreground" : "text-muted-foreground hover:bg-muted hover:text-foreground",
              )}
            >
              <Icon className="size-4" />
              {item.label}
              {item.id === "sources" && sources.length ? <span className="sr-only">, {sources.length}</span> : null}
              {item.id === "actions" && actions.length ? <span className="sr-only">, {actions.length}</span> : null}
            </button>
          );
        })}
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto p-4">
        {tab === "context" ? (
          <div className="space-y-4">
            <section>
              <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Active scope</p>
              {thread.scope_mode === "portfolio" ? (
                <div className="mt-2 rounded-xl border border-primary/20 bg-primary/5 p-3">
                  <div className="flex items-center gap-2 font-medium"><BriefcaseBusiness className="size-4 text-primary" />Full portfolio</div>
                  <p className="mt-1 text-xs leading-5 text-muted-foreground">Answers may use every visible project in your portfolio.</p>
                </div>
              ) : (
                <div className="mt-2 space-y-2">
                  {selectedProjects.map((project) => (
                    <Link key={project.project_id} href={`/projects/${encodeURIComponent(project.project_id)}`} className="block rounded-xl border p-3 transition-colors hover:border-primary/30 hover:bg-accent/30">
                      <div className="flex items-start justify-between gap-2">
                        <div className="min-w-0"><p className="truncate font-medium">{project.client_name || project.project_id}</p><p className="truncate font-mono text-[11px] text-muted-foreground">{project.project_id}</p></div>
                        <Badge variant="outline">{project.project_status}</Badge>
                      </div>
                      <p className="mt-2 text-xs text-muted-foreground">{project.current_milestone_display || project.current_milestone || "No milestone"}</p>
                    </Link>
                  ))}
                </div>
              )}
              <div className="mt-3 flex gap-2">
                <Button type="button" size="sm" variant="outline" className="flex-1" onClick={onEditProjects}>Edit projects</Button>
                {thread.scope_mode !== "portfolio" ? <Button type="button" size="sm" variant="ghost" onClick={onPortfolio}>Portfolio</Button> : null}
              </div>
            </section>
            <section className="rounded-xl border p-3">
              <div className="flex items-center justify-between gap-3">
                <div><p className="text-sm font-medium">Web Search</p><p className="mt-0.5 text-xs text-muted-foreground">External sources for this thread</p></div>
                <Badge variant={thread.web_enabled ? "default" : "secondary"}>{thread.web_enabled ? "On" : "Off"}</Badge>
              </div>
            </section>
          </div>
        ) : null}

        {tab === "sources" ? (
          <div className="space-y-2">
            <div>
              <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                {sourceIds === null ? "Verified sources" : "Sources for this response"}
              </p>
              {sourceIds !== null ? <p className="mt-1 text-xs text-muted-foreground">Only evidence cited by the selected answer is shown.</p> : null}
            </div>
            {sources.length ? sources.map((source) => {
              const href = assistantSourceHref(source);
              const external = isExternalAssistantSource(source);
              const SourceIcon = external ? Globe2 : source.kind === "email" ? Mail : source.project_id ? FolderKanban : Database;
              const content = (
                <div className="flex items-start gap-2">
                  <SourceIcon className={cn("mt-0.5 size-4 shrink-0", external ? "text-info" : "text-primary")} />
                  <div className="min-w-0 flex-1">
                    <p className="font-medium leading-5">{source.title}</p>
                    <div className="mt-0.5 flex flex-wrap gap-x-2 text-[11px] text-muted-foreground">
                      {source.kind ? <span className="capitalize">{source.kind.replaceAll("_", " ")}</span> : null}
                      {source.project_id ? <span className="font-mono">{source.project_id}</span> : null}
                      {source.occurred_at ? <span>{new Date(source.occurred_at).toLocaleString()}</span> : null}
                    </div>
                    {source.snippet ? <p className="mt-1 line-clamp-3 text-xs leading-5 text-muted-foreground">{source.snippet}</p> : null}
                  </div>
                  {href ? <ExternalLink className="size-3.5 shrink-0 text-muted-foreground" /> : null}
                </div>
              );
              if (!href) {
                return <div key={source.source_id} id={`source-${source.source_id}`} className="rounded-xl border p-3">{content}</div>;
              }
              return (
                <a
                  key={source.source_id}
                  id={`source-${source.source_id}`}
                  href={href}
                  target={external ? "_blank" : undefined}
                  rel={external ? "noreferrer" : undefined}
                  className="block rounded-xl border p-3 transition-colors hover:border-primary/30 hover:bg-accent/30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  {content}
                </a>
              );
            }) : <p className="rounded-xl border border-dashed p-4 text-sm leading-6 text-muted-foreground">No verified sources have been used in this conversation.</p>}
          </div>
        ) : null}

        {tab === "activity" ? (
          <div className="space-y-3">
            <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Recent activity</p>
            {activity.length ? activity.slice().reverse().map((entry, index) => (
              <div key={`${entry}-${index}`} className="flex gap-3 text-sm"><span className="mt-2 size-2 shrink-0 rounded-full bg-primary" /><p className="leading-6 text-muted-foreground">{entry}</p></div>
            )) : <p className="rounded-xl border border-dashed p-4 text-sm text-muted-foreground">Activity appears here while the assistant works.</p>}
          </div>
        ) : null}

        {tab === "actions" ? (
          <div className="space-y-3">
            <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Pending actions</p>
            {actions.length ? actions.map((action) => (
              action.type === "operator_update" ? (
                <div key={action.operation_id || `intake-${action.project_id}`} className="rounded-xl border border-warning/30 bg-warning/5 p-3">
                  <div className="flex items-start justify-between gap-2">
                    <div>
                      <p className="font-medium">Milestone update</p>
                      <p className="mt-1 font-mono text-[11px] text-muted-foreground">{action.project_id}</p>
                    </div>
                    <Badge variant="outline">{action.kind === "evaluator_aligned" ? "Aligned" : "OVERRIDE"}</Badge>
                  </div>
                  <p className="mt-3 text-xs leading-5 text-muted-foreground">Confirm the operator-update card in the conversation. Nothing is written until you click Confirm.</p>
                </div>
              ) : (
              <div key={action.action_id} className="rounded-xl border border-warning/30 bg-warning/5 p-3">
                <div className="flex items-start justify-between gap-2"><div><p className="font-medium">{action.action_type.replaceAll("_", " ")}</p><p className="mt-1 font-mono text-[11px] text-muted-foreground">{action.target_id}</p></div><Badge variant="outline" className="capitalize">{action.risk}</Badge></div>
                <p className="mt-3 text-xs leading-5 text-muted-foreground">Review the action card in the conversation before approving or rejecting it.</p>
              </div>
              )
            )) : <p className="rounded-xl border border-dashed p-4 text-sm leading-6 text-muted-foreground">No pending actions. The assistant cannot approve an action on your behalf.</p>}
          </div>
        ) : null}
      </div>
    </aside>
  );
}
