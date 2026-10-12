"use client";

import { useEffect, useMemo, useState } from "react";
import { BriefcaseBusiness, Loader2, Search, Sparkles } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { listAssistantThreads } from "@/lib/assistant-api";
import type { AssistantCapabilities, AssistantThread } from "@/lib/assistant-types";
import type { ProjectSummary } from "@/lib/types";
import { cn } from "@/lib/utils";

export function ResumeDialog({
  open,
  initialSearch = "",
  onOpenChange,
  onSelect,
}: {
  open: boolean;
  initialSearch?: string;
  onOpenChange: (open: boolean) => void;
  onSelect: (thread: AssistantThread) => void;
}) {
  const [draftSearch, setDraftSearch] = useState<string | null>(null);
  const search = draftSearch ?? initialSearch;
  const [threads, setThreads] = useState<AssistantThread[]>([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!open) return;
    const timer = window.setTimeout(() => {
      setLoading(true);
      void listAssistantThreads({ search, limit: 50 })
        .then((page) => setThreads(page.items))
        .finally(() => setLoading(false));
    }, search ? 200 : 0);
    return () => window.clearTimeout(timer);
  }, [open, search]);

  function handleOpenChange(nextOpen: boolean) {
    if (!nextOpen) setDraftSearch(null);
    onOpenChange(nextOpen);
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="max-h-[85dvh] max-w-2xl overflow-hidden p-0">
        <DialogHeader className="border-b p-5 pr-16">
          <DialogTitle>Resume a conversation</DialogTitle>
          <DialogDescription>Search by title or the latest message, then reopen the saved scope.</DialogDescription>
        </DialogHeader>
        <div className="p-4">
          <div className="relative">
            <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input value={search} onChange={(event) => setDraftSearch(event.target.value)} autoFocus placeholder="Search sessions" className="pl-9" />
          </div>
        </div>
        <div className="min-h-48 max-h-[55dvh] overflow-y-auto border-t p-2">
          {loading ? <div className="flex items-center justify-center gap-2 py-12 text-sm text-muted-foreground"><Loader2 className="size-4 animate-spin" />Searching…</div> : null}
          {!loading && !threads.length ? <p className="p-8 text-center text-sm text-muted-foreground">No conversations match this search.</p> : null}
          {!loading ? threads.map((thread) => (
            <button
              key={thread.thread_id}
              type="button"
              onClick={() => { onSelect(thread); handleOpenChange(false); }}
              className="min-h-20 w-full rounded-xl border border-transparent px-4 py-3 text-left transition-colors hover:border-border hover:bg-muted focus-visible:outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
            >
              <span className="flex items-start justify-between gap-3">
                <span className="min-w-0"><span className="block truncate font-semibold">{thread.title}</span><span className="mt-1 block truncate text-sm text-muted-foreground">{thread.last_message || "No messages yet"}</span></span>
                <span className="shrink-0 text-xs text-muted-foreground">{new Date(thread.updated_at).toLocaleDateString()}</span>
              </span>
              <span className="mt-2 block text-xs text-muted-foreground">{thread.scope_mode === "portfolio" ? "Full portfolio" : `${thread.project_ids.length} project${thread.project_ids.length === 1 ? "" : "s"}`}</span>
            </button>
          )) : null}
        </div>
      </DialogContent>
    </Dialog>
  );
}

export function ProjectPickerDialog({
  open,
  projects,
  selectedIds,
  initialSearch = "",
  minimum = 1,
  mode = "scope",
  onOpenChange,
  onApply,
}: {
  open: boolean;
  projects: ProjectSummary[];
  selectedIds: string[];
  initialSearch?: string;
  minimum?: number;
  mode?: "scope" | "compare";
  onOpenChange: (open: boolean) => void;
  onApply: (projectIds: string[]) => void;
}) {
  const [draftSearch, setDraftSearch] = useState<string | null>(null);
  const [draftSelection, setDraftSelection] = useState<string[] | null>(null);
  const search = draftSearch ?? initialSearch;
  const selection = draftSelection ?? selectedIds;
  const filtered = useMemo(() => {
    const needle = search.trim().toLowerCase();
    if (!needle) return projects;
    return projects.filter((project) => [project.project_id, project.client_name, project.market]
      .filter(Boolean).some((value) => String(value).toLowerCase().includes(needle)));
  }, [projects, search]);

  function toggle(projectId: string) {
    setDraftSelection((current) => {
      const source = current ?? selectedIds;
      return source.includes(projectId)
        ? source.filter((item) => item !== projectId)
        : [...source, projectId];
    });
  }

  function handleOpenChange(nextOpen: boolean) {
    if (!nextOpen) {
      setDraftSelection(null);
      setDraftSearch(null);
    }
    onOpenChange(nextOpen);
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="flex max-h-[88dvh] max-w-2xl flex-col overflow-hidden p-0">
        <DialogHeader className="border-b p-5 pr-16">
          <DialogTitle>{mode === "compare" ? "Choose projects to compare" : "Set project context"}</DialogTitle>
          <DialogDescription>{mode === "compare" ? "Select at least two visible projects. The thread context will be updated before comparison." : "Select one or more projects to keep answers focused and auditable."}</DialogDescription>
        </DialogHeader>
        <div className="p-4">
          <div className="relative"><Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" /><Input value={search} onChange={(event) => setDraftSearch(event.target.value)} placeholder="Search project, client, or market" className="pl-9" /></div>
          <div className="mt-3 flex items-center justify-between text-xs text-muted-foreground"><span>{selection.length} selected</span>{selection.length ? <button type="button" className="min-h-8 rounded-md px-2 font-medium text-primary hover:bg-accent" onClick={() => setDraftSelection([])}>Clear</button> : null}</div>
        </div>
        <div className="min-h-48 flex-1 overflow-y-auto border-y p-2">
          {filtered.length ? filtered.map((project) => {
            const checked = selection.includes(project.project_id);
            return (
              <label key={project.project_id} className={cn("flex min-h-16 cursor-pointer items-center gap-3 rounded-xl border px-3 py-2 transition-colors", checked ? "border-primary/35 bg-primary/7" : "border-transparent hover:border-border hover:bg-muted")}>
                <input type="checkbox" checked={checked} onChange={() => toggle(project.project_id)} className="size-4 accent-primary" />
                <span className="min-w-0 flex-1"><span className="block truncate text-sm font-semibold">{project.client_name || project.project_id}</span><span className="mt-0.5 block truncate font-mono text-xs text-muted-foreground">{project.project_id} · {project.market || "No market"}</span></span>
                <span className="text-xs text-muted-foreground">{project.project_status}</span>
              </label>
            );
          }) : <p className="p-8 text-center text-sm text-muted-foreground">No projects match this search.</p>}
        </div>
        <DialogFooter className="m-0 p-4">
          <Button type="button" variant="outline" onClick={() => handleOpenChange(false)}>Cancel</Button>
          <Button type="button" disabled={selection.length < minimum} onClick={() => { onApply(selection); handleOpenChange(false); }}>
            {mode === "compare" ? "Compare projects" : "Apply context"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function HelpDialog({
  open,
  capabilities,
  onOpenChange,
}: {
  open: boolean;
  capabilities: AssistantCapabilities | null;
  onOpenChange: (open: boolean) => void;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[85dvh] max-w-2xl overflow-y-auto">
        <DialogHeader>
          <div className="flex size-11 items-center justify-center rounded-xl bg-primary/10 text-primary"><Sparkles className="size-5" /></div>
          <DialogTitle className="mt-3">AI Workspace commands</DialogTitle>
          <DialogDescription>Natural language works for every major task. Slash commands provide deterministic shortcuts.</DialogDescription>
        </DialogHeader>
        <div className="mt-5 space-y-2">
          {(capabilities?.commands || []).map((command) => (
            <div key={command.name} className="grid gap-2 rounded-xl border p-3 sm:grid-cols-[9rem_1fr]"><code className="font-mono text-sm font-semibold text-primary">{command.usage}</code><p className="text-sm leading-6 text-muted-foreground">{command.description}</p></div>
          ))}
        </div>
        <div className="mt-5 rounded-xl border bg-muted/40 p-4">
          <div className="flex items-center gap-2 font-medium"><BriefcaseBusiness className="size-4" />Permissions and safety</div>
          <ul className="mt-2 list-disc space-y-1 pl-5 text-sm leading-6 text-muted-foreground">
            <li>Threads are private to your signed-in account.</li>
            <li>Drafts are created for review and are never sent automatically.</li>
            <li>Business changes require an approval card; the assistant cannot approve its own action.</li>
            <li>Web Search runs only when the thread toggle is on and a provider is configured.</li>
          </ul>
        </div>
      </DialogContent>
    </Dialog>
  );
}
