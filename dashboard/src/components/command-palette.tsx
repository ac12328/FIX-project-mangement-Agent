"use client";

import { useDeferredValue, useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import {
  Activity,
  Bot,
  Building2,
  FileText,
  FolderKanban,
  LoaderCircle,
  Search,
  Ticket,
} from "lucide-react";

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { useAuth } from "@/lib/auth-context";
import { searchUi, uiV2Keys } from "@/lib/ui-v2-api";
import type { UiSearchItem } from "@/lib/ui-v2-types";
import { cn } from "@/lib/utils";

const QUICK_LINKS: Array<UiSearchItem & { permission?: string }> = [
  { id: "dashboard", type: "page", title: "Today", subtitle: "Today's work", href: "/", keywords: ["dashboard", "home"], score: 0 },
  { id: "ask", type: "page", title: "Ask", subtitle: "AI workspace", href: "/ask", keywords: ["queries", "assistant"], score: 0, permission: "assistant.read" },
  { id: "mail", type: "page", title: "Mail", subtitle: "Inbox and drafts", href: "/mail", keywords: ["inbox", "drafts", "compose"], score: 0, permission: "business.write" },
  { id: "projects", type: "page", title: "Projects", subtitle: "Project list", href: "/projects", keywords: [], score: 0 },
  { id: "operations", type: "page", title: "Operations", subtitle: "Health, timeline, and completed", href: "/operations", keywords: ["portfolio", "timeline", "completed"], score: 0 },
  { id: "reviews", type: "page", title: "Reviews", subtitle: "Pending field updates", href: "/reviews", keywords: [], score: 0, permission: "business.write" },
  { id: "support", type: "page", title: "Desk", subtitle: "Support tickets", href: "/support", keywords: ["support", "tickets"], score: 0, permission: "business.write" },
  { id: "counterparties", type: "page", title: "Counterparties", subtitle: "Clients and vendors", href: "/counterparties", keywords: ["clients", "vendors"], score: 0 },
];

const TYPE_ICONS = {
  page: FileText,
  project: FolderKanban,
  client: Building2,
  vendor: Building2,
  ticket: Ticket,
} as const;

export function CommandPalette({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const router = useRouter();
  const inputRef = useRef<HTMLInputElement>(null);
  const { hasPermission } = useAuth();
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);
  const deferredQuery = useDeferredValue(query.trim());

  const searchQuery = useQuery({
    queryKey: uiV2Keys.search(deferredQuery),
    queryFn: () => searchUi(deferredQuery),
    enabled: open && deferredQuery.length > 0,
    staleTime: 60_000,
  });

  const quickLinks = useMemo(
    () => QUICK_LINKS.filter((item) => !item.permission || hasPermission(item.permission)),
    [hasPermission],
  );
  const items = deferredQuery ? (searchQuery.data?.items ?? []) : quickLinks;
  const currentIndex = Math.min(activeIndex, Math.max(items.length - 1, 0));

  useEffect(() => {
    if (!open) return;
    const id = window.setTimeout(() => inputRef.current?.focus(), 0);
    return () => window.clearTimeout(id);
  }, [open]);

  function handleOpenChange(nextOpen: boolean) {
    if (!nextOpen) {
      setQuery("");
      setActiveIndex(0);
    }
    onOpenChange(nextOpen);
  }

  function navigate(item: UiSearchItem) {
    handleOpenChange(false);
    router.push(item.href);
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="top-[12vh] max-w-2xl self-start overflow-hidden p-0" showClose={false}>
        <DialogHeader className="sr-only">
          <DialogTitle>Search workspace</DialogTitle>
          <DialogDescription>Search projects, parties, support tickets, and dashboard pages.</DialogDescription>
        </DialogHeader>
        <div className="flex items-center gap-3 border-b px-4">
          <Search className="size-5 shrink-0 text-muted-foreground" aria-hidden="true" />
          <Input
            ref={inputRef}
            value={query}
            onChange={(event) => { setQuery(event.target.value); setActiveIndex(0); }}
            onKeyDown={(event) => {
              if (event.key === "ArrowDown") {
                event.preventDefault();
                setActiveIndex((index) => Math.min(index + 1, Math.max(items.length - 1, 0)));
              } else if (event.key === "ArrowUp") {
                event.preventDefault();
                setActiveIndex((index) => Math.max(index - 1, 0));
              } else if (event.key === "Enter" && items[currentIndex]) {
                event.preventDefault();
                navigate(items[currentIndex]);
              }
            }}
            role="combobox"
            aria-expanded={items.length > 0}
            aria-controls="workspace-command-results"
            aria-activedescendant={items[currentIndex] ? `command-${items[currentIndex].type}-${items[currentIndex].id}` : undefined}
            placeholder="Search projects, clients, vendors, tickets, or pages"
            className="h-14 border-0 bg-transparent px-0 text-base shadow-none focus-visible:ring-0"
          />
          <kbd className="hidden rounded-md border bg-muted px-2 py-1 font-mono text-xs text-muted-foreground sm:inline-flex">Esc</kbd>
        </div>

        <div id="workspace-command-results" role="listbox" className="max-h-[min(26rem,65vh)] overflow-y-auto p-2">
          {searchQuery.isFetching && deferredQuery ? (
            <div className="flex min-h-32 items-center justify-center gap-2 text-sm text-muted-foreground" role="status">
              <LoaderCircle className="size-4 animate-spin" /> Searching workspace
            </div>
          ) : null}
          {!searchQuery.isFetching && searchQuery.isError ? (
            <div className="p-5 text-sm text-destructive">Search is temporarily unavailable. Try again.</div>
          ) : null}
          {!searchQuery.isFetching && !searchQuery.isError && items.length === 0 ? (
            <div className="flex min-h-32 flex-col items-center justify-center gap-2 px-5 text-center">
              <Bot className="size-6 text-muted-foreground" />
              <p className="text-sm font-medium">No matching workspace items</p>
              <p className="text-xs text-muted-foreground">Try a project ID, organisation name, or ticket title.</p>
            </div>
          ) : null}
          {!searchQuery.isFetching && !searchQuery.isError && items.length > 0 ? (
            <div className="space-y-1">
              <p className="px-3 pb-1 pt-2 text-xs font-semibold uppercase tracking-[0.14em] text-muted-foreground">
                {deferredQuery ? "Search results" : "Quick access"}
              </p>
              {items.map((item, index) => {
                const Icon = TYPE_ICONS[item.type] ?? Activity;
                const active = index === currentIndex;
                return (
                  <button
                    key={`${item.type}-${item.id}`}
                    id={`command-${item.type}-${item.id}`}
                    role="option"
                    aria-selected={active}
                    type="button"
                    onPointerMove={() => setActiveIndex(index)}
                    onClick={() => navigate(item)}
                    className={cn(
                      "flex min-h-14 w-full items-center gap-3 rounded-lg px-3 text-left transition-colors duration-150",
                      active ? "bg-accent text-accent-foreground" : "hover:bg-muted",
                    )}
                  >
                    <span className="flex size-9 shrink-0 items-center justify-center rounded-lg border bg-background text-muted-foreground">
                      <Icon className="size-4" />
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-sm font-medium">{item.title}</span>
                      {item.subtitle ? <span className="block truncate text-xs text-muted-foreground">{item.subtitle}</span> : null}
                    </span>
                    <span className="rounded-md bg-muted px-2 py-1 text-xs capitalize text-muted-foreground">{item.type}</span>
                  </button>
                );
              })}
            </div>
          ) : null}
        </div>
      </DialogContent>
    </Dialog>
  );
}
