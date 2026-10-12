"use client";

import { FormEvent, useState } from "react";
import {
  Archive,
  ArchiveRestore,
  Check,
  Loader2,
  MessageSquarePlus,
  MoreHorizontal,
  PanelLeftClose,
  PanelLeftOpen,
  Pencil,
  Search,
  Trash2,
  X,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { AssistantThread } from "@/lib/assistant-types";
import { cn } from "@/lib/utils";

interface ThreadSidebarProps {
  threads: AssistantThread[];
  selectedThreadId?: string | null;
  search: string;
  archived: boolean;
  loading?: boolean;
  loadingMore?: boolean;
  hasMore?: boolean;
  collapsed?: boolean;
  collapsible?: boolean;
  onSearch: (value: string) => void;
  onArchivedChange: (value: boolean) => void;
  onSelect: (thread: AssistantThread) => void;
  onNew: () => void;
  onRename: (thread: AssistantThread, title: string) => void;
  onArchive: (thread: AssistantThread, archived: boolean) => void;
  onDelete: (thread: AssistantThread) => Promise<void> | void;
  onLoadMore: () => void;
  onCollapsedChange?: (collapsed: boolean) => void;
}

function ThreadRow({
  thread,
  active,
  onSelect,
  onRename,
  onArchive,
  onDelete,
}: {
  thread: AssistantThread;
  active: boolean;
  onSelect: () => void;
  onRename: (title: string) => void;
  onArchive: () => void;
  onDelete: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(thread.title);

  function submit(event: FormEvent) {
    event.preventDefault();
    const next = title.trim();
    if (next && next !== thread.title) onRename(next);
    else setTitle(thread.title);
    setEditing(false);
  }

  if (editing) {
    return (
      <form onSubmit={submit} className="rounded-lg border border-primary/35 bg-card p-2">
        <label className="sr-only" htmlFor={`rename-${thread.thread_id}`}>Conversation title</label>
        <Input
          id={`rename-${thread.thread_id}`}
          value={title}
          maxLength={120}
          autoFocus
          onChange={(event) => setTitle(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              setTitle(thread.title);
              setEditing(false);
            }
          }}
        />
        <div className="mt-2 flex justify-end gap-1">
          <Button type="button" size="icon-sm" variant="ghost" aria-label="Cancel rename" onClick={() => setEditing(false)}><X /></Button>
          <Button type="submit" size="icon-sm" aria-label="Save title"><Check /></Button>
        </div>
      </form>
    );
  }

  return (
    <div className={cn(
      "group rounded-lg transition-colors",
      active ? "bg-sidebar-accent text-sidebar-accent-foreground" : "hover:bg-sidebar-accent/65",
    )}>
      <button
        type="button"
        onClick={onSelect}
        className="min-h-14 w-full px-3 pb-1 pt-2.5 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
      >
        <span className="block truncate text-sm font-medium">{thread.title}</span>
        <span className="mt-1 block truncate text-xs text-muted-foreground">
          {thread.last_message || (thread.scope_mode === "portfolio" ? "Full portfolio" : `${thread.project_ids.length} project context`)}
        </span>
      </button>
      <div className="flex min-h-9 items-center justify-between px-2 pb-1.5">
        <span className="px-1 text-[11px] text-muted-foreground" title={new Date(thread.updated_at).toLocaleString()}>
          {new Date(thread.updated_at).toLocaleDateString([], { month: "short", day: "numeric" })}
        </span>
        <DropdownMenu>
          <DropdownMenuTrigger
            render={<Button type="button" size="icon-sm" variant="ghost" className="size-9 opacity-100 lg:opacity-0 lg:group-hover:opacity-100 lg:group-focus-within:opacity-100" />}
            aria-label={`Open actions for ${thread.title}`}
          >
            <MoreHorizontal className="size-4" />
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-44">
            <DropdownMenuItem onClick={() => { setTitle(thread.title); setEditing(true); }}>
              <Pencil /> Rename
            </DropdownMenuItem>
            <DropdownMenuItem onClick={onArchive}>
              {thread.archived_at ? <ArchiveRestore /> : <Archive />}
              {thread.archived_at ? "Restore" : "Archive"}
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem variant="destructive" onClick={onDelete}>
              <Trash2 /> Delete
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </div>
  );
}

export function ThreadSidebar({
  threads,
  selectedThreadId,
  search,
  archived,
  loading = false,
  loadingMore = false,
  hasMore = false,
  collapsed = false,
  collapsible = false,
  onSearch,
  onArchivedChange,
  onSelect,
  onNew,
  onRename,
  onArchive,
  onDelete,
  onLoadMore,
  onCollapsedChange,
}: ThreadSidebarProps) {
  const [deleteTarget, setDeleteTarget] = useState<AssistantThread | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  function closeDeleteDialog() {
    if (deleting) return;
    setDeleteTarget(null);
    setDeleteError(null);
  }

  async function confirmDelete() {
    if (!deleteTarget || deleting) return;
    setDeleting(true);
    setDeleteError(null);
    try {
      await onDelete(deleteTarget);
      setDeleteTarget(null);
    } catch (caught) {
      setDeleteError(caught instanceof Error ? caught.message : "The conversation could not be deleted.");
    } finally {
      setDeleting(false);
    }
  }

  if (collapsed && collapsible) {
    return (
      <aside className="flex h-full min-h-0 flex-col items-center gap-1 border-r-0 bg-sidebar px-1.5 py-3 text-sidebar-foreground">
        <Tooltip>
          <TooltipTrigger render={<Button type="button" size="icon-sm" onClick={onNew} aria-label="New conversation" />}>
            <MessageSquarePlus />
          </TooltipTrigger>
          <TooltipContent side="right">New conversation</TooltipContent>
        </Tooltip>
        <Tooltip>
          <TooltipTrigger
            render={<Button type="button" size="icon-sm" variant="ghost" onClick={() => onCollapsedChange?.(false)} aria-label="Expand conversation history" />}
          >
            <PanelLeftOpen />
          </TooltipTrigger>
          <TooltipContent side="right">Expand history</TooltipContent>
        </Tooltip>
      </aside>
    );
  }

  return (
    <>
      <aside className="flex h-full min-h-0 flex-col bg-sidebar text-sidebar-foreground">
        <div className="border-b border-sidebar-border p-3">
          <div className="flex gap-1">
            <Button className="min-w-0 flex-1 justify-start" onClick={onNew}>
              <MessageSquarePlus />
              <span className="truncate">New conversation</span>
            </Button>
            {collapsible ? (
              <Tooltip>
                <TooltipTrigger
                  render={<Button type="button" size="icon-sm" variant="ghost" aria-label="Collapse conversation history" onClick={() => onCollapsedChange?.(true)} />}
                >
                  <PanelLeftClose />
                </TooltipTrigger>
                <TooltipContent>Collapse history</TooltipContent>
              </Tooltip>
            ) : null}
          </div>
          <div className="relative mt-3">
            <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input value={search} onChange={(event) => onSearch(event.target.value)} placeholder="Search conversations" className="bg-background pl-9" />
          </div>
          <div className="mt-3 grid grid-cols-2 rounded-lg bg-muted p-1" aria-label="Conversation status filter">
            <button
              type="button"
              aria-pressed={!archived}
              onClick={() => onArchivedChange(false)}
              className={cn("min-h-9 rounded-md px-3 text-xs font-semibold transition-colors", !archived ? "bg-background text-foreground shadow-sm" : "text-muted-foreground")}
            >
              Active
            </button>
            <button
              type="button"
              aria-pressed={archived}
              onClick={() => onArchivedChange(true)}
              className={cn("min-h-9 rounded-md px-3 text-xs font-semibold transition-colors", archived ? "bg-background text-foreground shadow-sm" : "text-muted-foreground")}
            >
              Archived
            </button>
          </div>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto p-2" aria-busy={loading}>
          {loading ? (
            <div className="flex items-center justify-center gap-2 py-10 text-sm text-muted-foreground" role="status">
              <Loader2 className="size-4 animate-spin" />
              Loading threads…
            </div>
          ) : threads.length ? (
            <div className="space-y-1">
              {threads.map((thread) => (
                <ThreadRow
                  key={thread.thread_id}
                  thread={thread}
                  active={thread.thread_id === selectedThreadId}
                  onSelect={() => onSelect(thread)}
                  onRename={(title) => onRename(thread, title)}
                  onArchive={() => onArchive(thread, !Boolean(thread.archived_at))}
                  onDelete={() => { setDeleteError(null); setDeleteTarget(thread); }}
                />
              ))}
              {hasMore ? (
                <Button className="w-full" type="button" size="sm" variant="ghost" onClick={onLoadMore} disabled={loadingMore}>
                  {loadingMore ? <Loader2 className="animate-spin" /> : null}
                  Load more
                </Button>
              ) : null}
            </div>
          ) : (
            <div className="px-4 py-10 text-center text-sm text-muted-foreground">
              {search ? "No matching conversations." : archived ? "No archived conversations." : "Start your first conversation."}
            </div>
          )}
        </div>
      </aside>

      <Dialog open={Boolean(deleteTarget)} onOpenChange={(open) => { if (!open) closeDeleteDialog(); }}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>Delete conversation?</DialogTitle>
            <DialogDescription>
              “{deleteTarget?.title}” and its messages will be permanently deleted. This action cannot be undone.
            </DialogDescription>
          </DialogHeader>
          {deleteError ? <p role="alert" className="mt-4 text-sm leading-6 text-destructive">{deleteError}</p> : null}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={closeDeleteDialog} disabled={deleting}>Cancel</Button>
            <Button type="button" variant="destructive" onClick={() => void confirmDelete()} disabled={deleting}>
              {deleting ? <Loader2 className="animate-spin" /> : <Trash2 />}
              {deleting ? "Deleting…" : "Delete conversation"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
