"use client";

import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { createPortal } from "react-dom";
import { useQuery } from "@tanstack/react-query";
import { Dialog as DialogPrimitive } from "@base-ui/react/dialog";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useTheme } from "next-themes";
import {
  Activity,
  BookOpen,
  Building2,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  ChevronsLeft,
  ChevronsRight,
  FolderKanban,
  GitBranch,
  Inbox,
  LayoutDashboard,
  LineChart,
  Moon,
  ScrollText,
  ShieldCheck,
  Sparkles,
  Sun,
  Wrench,
  X,
  type LucideIcon,
} from "lucide-react";

import { LLMStatusIndicator } from "@/components/llm-status-indicator";
import { SidebarSession } from "@/components/sidebar-session";
import { Button } from "@/components/ui/button";
import { useAuth } from "@/lib/auth-context";
import { useSidebar } from "@/lib/sidebar-context";
import { getUiOverview, uiV2Keys } from "@/lib/ui-v2-api";
import { cn } from "@/lib/utils";

interface NavItem {
  href: string;
  label: string;
  icon: LucideIcon;
  permission?: string;
  badgeKind?: "review" | "draft";
  children?: Array<Omit<NavItem, "icon" | "children">>;
}

interface NavGroup {
  heading: string;
  items: NavItem[];
}

const NAV_GROUPS: NavGroup[] = [
  {
    heading: "Daily",
    items: [
      { href: "/", label: "Today", icon: LayoutDashboard, permission: "shared.read" },
      { href: "/ask", label: "Ask", icon: Sparkles, permission: "assistant.read" },
      { href: "/mail", label: "Mail", icon: Inbox, permission: "business.write", badgeKind: "draft" },
      { href: "/projects", label: "Projects", icon: FolderKanban, permission: "shared.read" },
      { href: "/operations", label: "Operations", icon: Activity, permission: "shared.read" },
      { href: "/reviews", label: "Reviews", icon: CheckCircle2, permission: "business.write", badgeKind: "review" },
      { href: "/support", label: "Desk", icon: Wrench, permission: "business.write" },
      { href: "/counterparties", label: "Counterparties", icon: Building2, permission: "shared.read" },
    ],
  },
  {
    heading: "System",
    items: [
      { href: "/activity", label: "Activity", icon: ScrollText, permission: "admin.users" },
      { href: "/llm-analytics", label: "LLM analytics", icon: LineChart, permission: "admin.llm_config" },
      { href: "/traces", label: "Traces", icon: GitBranch, permission: "traces.sanitized" },
      {
        href: "/learning",
        label: "Learning",
        icon: BookOpen,
        permission: "admin.learning_governance",
        children: [
          { href: "/learning", label: "Overview", permission: "admin.learning_governance" },
          { href: "/learning/candidates", label: "Candidates", permission: "admin.learning_governance" },
          { href: "/learning/memory", label: "Memory", permission: "admin.learning_governance" },
          { href: "/learning/conflicts", label: "Conflicts", permission: "admin.learning_governance" },
          { href: "/learning/jobs", label: "Jobs", permission: "admin.learning_governance" },
          { href: "/learning/audit", label: "Audit", permission: "admin.learning_governance" },
        ],
      },
      {
        href: "/admin",
        label: "Admin",
        icon: ShieldCheck,
        permission: "admin.users",
        children: [
          { href: "/admin", label: "Users", permission: "admin.users" },
          { href: "/llm-config", label: "LLM configuration", permission: "admin.llm_config" },
          { href: "/conversation", label: "Agent context", permission: "admin.agent_context" },
        ],
      },
    ],
  },
];

function routeIsActive(pathname: string, href: string) {
  return href === "/" ? pathname === "/" : pathname === href || pathname.startsWith(`${href}/`);
}

function BadgeCount({ count }: { count: number | undefined }) {
  if (!count) return null;
  return (
    <span className="ml-auto flex h-5 min-w-5 items-center justify-center rounded-md bg-primary/12 px-1.5 font-mono text-xs font-semibold text-primary">
      {count > 99 ? "99+" : count}
    </span>
  );
}

function NavContent({
  collapsed = false,
  dense = false,
  flyout = false,
  onNavigate,
}: {
  collapsed?: boolean;
  dense?: boolean;
  flyout?: boolean;
  onNavigate?: () => void;
}) {
  const pathname = usePathname();
  const { hasPermission } = useAuth();
  const canWrite = hasPermission("business.write");
  const [openMenu, setOpenMenu] = useState<{ href: string; top: number; left: number } | null>(null);
  const [menuPathname, setMenuPathname] = useState(pathname);
  const menuRef = useRef<HTMLDivElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const overview = useQuery({
    queryKey: uiV2Keys.overview(),
    queryFn: getUiOverview,
    enabled: canWrite,
    staleTime: 30_000,
  });
  // Preview items are capped; never infer full queue counts from them.
  const sectionStatus = overview.data?.section_status;
  const queueUnavailable = overview.isError || (Array.isArray(sectionStatus) && sectionStatus.some(
    (section) => section.section === "action_queue" && section.state === "error",
  ));
  const counts = queueUnavailable ? null : overview.data?.action_queue_counts;

  // Reset only this component's transient menu state before rendering the new
  // route, rather than scheduling a second render from an effect.
  if (menuPathname !== pathname) {
    setMenuPathname(pathname);
    setOpenMenu(null);
  }

  useEffect(() => {
    if (!openMenu) return;
    function close(event: Event) {
      if (event.type === "keydown" && (event as KeyboardEvent).key !== "Escape") return;
      const target = event.target as Node;
      if (menuRef.current?.contains(target) || panelRef.current?.contains(target)) return;
      setOpenMenu(null);
    }
    document.addEventListener("pointerdown", close);
    document.addEventListener("keydown", close);
    return () => {
      document.removeEventListener("pointerdown", close);
      document.removeEventListener("keydown", close);
    };
  }, [openMenu]);

  return (
    <nav aria-label="Primary navigation" className={cn("flex-1 px-2", dense ? "space-y-3 py-2" : "space-y-5 py-4")}>
      {NAV_GROUPS.map((group) => {
        const visible = group.items.filter((item) => !item.permission || hasPermission(item.permission));
        if (visible.length === 0) return null;
        return (
          <section key={group.heading} aria-labelledby={`nav-${group.heading.replaceAll(" ", "-").toLowerCase()}`}>
            {!collapsed ? (
              <h2 id={`nav-${group.heading.replaceAll(" ", "-").toLowerCase()}`} className={cn("px-2 text-xs font-semibold uppercase tracking-[0.14em] text-muted-foreground", dense ? "mb-1" : "mb-1.5")}>
                {group.heading}
              </h2>
            ) : null}
            <div className={dense ? "space-y-0.5" : "space-y-1"}>
              {visible.map((item) => {
                const Icon = item.icon;
                const active = routeIsActive(pathname, item.href);
                const count = item.badgeKind ? counts?.[item.badgeKind] : 0;
                const visibleChildren = item.children?.filter((child) => !child.permission || hasPermission(child.permission));

                if (visibleChildren?.length && flyout) {
                  const menuOpen = openMenu?.href === item.href;
                  return (
                    <div key={item.href} ref={menuOpen ? menuRef : undefined} className="relative">
                      <button
                        type="button"
                        aria-expanded={menuOpen}
                        aria-haspopup="true"
                        title={collapsed ? item.label : undefined}
                        aria-label={collapsed ? `${item.label} pages` : undefined}
                        onClick={(event) => {
                          if (menuOpen) {
                            setOpenMenu(null);
                            return;
                          }
                          const rect = event.currentTarget.getBoundingClientRect();
                          const estimated = visibleChildren.length * 44 + 16;
                          const top = Math.max(8, Math.min(rect.top, window.innerHeight - estimated - 8));
                          setOpenMenu({ href: item.href, top, left: rect.right + 8 });
                        }}
                        className={cn(
                          "flex w-full items-center gap-3 rounded-lg px-2.5 text-sm font-medium transition-colors duration-150",
                          dense ? "min-h-9" : "min-h-11",
                          collapsed ? "justify-center" : undefined,
                          active ? "bg-sidebar-accent text-sidebar-accent-foreground" : "text-muted-foreground hover:bg-muted hover:text-foreground",
                        )}
                      >
                        <Icon className="size-4 shrink-0" />
                        {collapsed ? null : <span className="min-w-0 flex-1 truncate text-left">{item.label}</span>}
                        {collapsed ? null : <ChevronRight className="size-4 shrink-0" />}
                      </button>
                      {menuOpen && openMenu ? createPortal(
                        <div
                          ref={panelRef}
                          role="region"
                          aria-label={`${item.label} pages`}
                          className="fixed z-50 w-56 rounded-xl border border-border bg-popover p-1.5 text-popover-foreground shadow-xl"
                          style={{ top: openMenu.top, left: openMenu.left }}
                        >
                          {visibleChildren.map((child) => {
                            const childActive = pathname === child.href || (child.href !== item.href && pathname.startsWith(`${child.href}/`));
                            return (
                              <Link
                                key={child.href}
                                href={child.href}
                                onClick={() => {
                                  setOpenMenu(null);
                                  onNavigate?.();
                                }}
                                aria-current={childActive ? "page" : undefined}
                                className={cn(
                                  "flex min-h-11 items-center rounded-lg px-3 text-sm transition-colors duration-150",
                                  childActive ? "bg-accent font-medium text-accent-foreground" : "text-popover-foreground hover:bg-muted",
                                )}
                              >
                                {child.label}
                              </Link>
                            );
                          })}
                        </div>,
                        document.body,
                      ) : null}
                    </div>
                  );
                }

                if (visibleChildren?.length && !collapsed) {
                  return (
                    <details key={item.href} open={active} className="group/nav-details">
                      <summary className={cn(
                        "flex cursor-pointer list-none items-center gap-3 rounded-lg px-2.5 text-sm font-medium transition-colors duration-150 [&::-webkit-details-marker]:hidden",
                        dense ? "min-h-9" : "min-h-11",
                        active ? "bg-sidebar-accent text-sidebar-accent-foreground" : "text-muted-foreground hover:bg-muted hover:text-foreground",
                      )}>
                        <Icon className="size-4 shrink-0" />
                        <span className="min-w-0 flex-1 truncate">{item.label}</span>
                        <ChevronDown className="size-4 transition-transform duration-200 group-open/nav-details:rotate-180" />
                      </summary>
                      <div className="ml-5 mt-1 space-y-1 border-l pl-3">
                        {visibleChildren.map((child) => {
                          const childActive = pathname === child.href || (child.href !== item.href && pathname.startsWith(`${child.href}/`));
                          return (
                            <Link
                              key={child.href}
                              href={child.href}
                              onClick={onNavigate}
                              aria-current={childActive ? "page" : undefined}
                              className={cn(
                                "flex items-center rounded-lg px-2.5 text-sm transition-colors duration-150",
                                dense ? "min-h-8" : "min-h-11",
                                childActive ? "bg-accent font-medium text-accent-foreground" : "text-muted-foreground hover:bg-muted hover:text-foreground",
                              )}
                            >
                              {child.label}
                            </Link>
                          );
                        })}
                      </div>
                    </details>
                  );
                }

                return (
                  <Link
                    key={item.href}
                    href={item.href}
                    title={collapsed ? item.label : undefined}
                    onClick={onNavigate}
                    aria-label={collapsed ? item.label : undefined}
                    aria-current={active ? "page" : undefined}
                    className={cn(
                      "flex items-center gap-3 rounded-lg px-2.5 text-sm font-medium transition-colors duration-150",
                      dense ? "min-h-9" : "min-h-11",
                      collapsed ? "justify-center" : undefined,
                      active ? "bg-sidebar-accent text-sidebar-accent-foreground" : "text-muted-foreground hover:bg-muted hover:text-foreground",
                    )}
                  >
                    <Icon className="size-4 shrink-0" />
                    {!collapsed ? <span className="min-w-0 flex-1 truncate">{item.label}</span> : null}
                    {!collapsed ? <BadgeCount count={count} /> : null}
                  </Link>
                );
              })}
            </div>
          </section>
        );
      })}
    </nav>
  );
}

function SidebarFooter({ collapsed, allowDesktopToggle = true }: { collapsed: boolean; allowDesktopToggle?: boolean }) {
  const { resolvedTheme, setTheme } = useTheme();
  const { toggleDesktop } = useSidebar();
  const mounted = useSyncExternalStore(() => () => undefined, () => true, () => false);
  const dark = mounted && resolvedTheme === "dark";

  return (
    <div className="border-t border-sidebar-border p-2">
      {!collapsed ? <LLMStatusIndicator /> : null}
      <SidebarSession collapsed={collapsed} />
      <div className={cn("mt-1 flex", collapsed ? "flex-col gap-1" : "gap-1")}>
        <Button
          type="button"
          variant="ghost"
          size={collapsed ? "icon" : "default"}
          onClick={() => setTheme(dark ? "light" : "dark")}
          aria-label={dark ? "Use light theme" : "Use dark theme"}
          title={collapsed ? (dark ? "Light theme" : "Dark theme") : undefined}
          className={cn(collapsed ? "w-full" : "min-w-0 flex-1 justify-start")}
        >
          {dark ? <Sun className="size-4" /> : <Moon className="size-4" />}
          {!collapsed ? <span className="truncate">{dark ? "Light theme" : "Dark theme"}</span> : null}
        </Button>
        {allowDesktopToggle ? (
          <Button
            type="button"
            variant="ghost"
            size="icon"
            onClick={toggleDesktop}
            aria-label={collapsed ? "Expand navigation" : "Collapse navigation"}
            title={collapsed ? "Expand navigation" : "Collapse navigation"}
            className="hidden w-full md:flex"
          >
            {collapsed ? <ChevronsRight className="size-4" /> : <ChevronsLeft className="size-4" />}
          </Button>
        ) : null}
      </div>
    </div>
  );
}

export function Sidebar() {
  const pathname = usePathname();
  const { mobileOpen, desktopCollapsed, closeMobile } = useSidebar();
  const forceCompact = pathname === "/ask";
  const collapsed = forceCompact || desktopCollapsed;

  useEffect(() => closeMobile(), [closeMobile, pathname]);

  return (
    <>
      <aside
        data-testid="primary-navigation-desktop"
        className={cn(
          "hidden h-full min-h-0 shrink-0 flex-col overflow-hidden border-r border-sidebar-border bg-sidebar text-sidebar-foreground transition-[width] duration-200 md:flex",
          collapsed ? "w-[72px]" : "w-64",
        )}
      >
        <div className="flex h-12 shrink-0 items-center border-b border-sidebar-border px-2">
          <Link
            href="/"
            title={collapsed ? "AgentFIXBPRO" : undefined}
            aria-label="AgentFIXBPRO home"
            className={cn(
              "flex min-h-11 min-w-0 items-center gap-2 rounded-lg focus-visible:outline-none focus-visible:ring-3 focus-visible:ring-ring/50",
              collapsed ? "w-full justify-center" : "px-1.5",
            )}
          >
            <span className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-primary font-mono text-xs font-bold text-primary-foreground">FX</span>
            {collapsed ? null : <span className="truncate font-heading text-sm font-semibold">AgentFIXBPRO</span>}
          </Link>
        </div>
        <div className="min-h-0 flex-1 overflow-hidden">
          <NavContent collapsed={collapsed} dense flyout />
        </div>
        <SidebarFooter collapsed={collapsed} allowDesktopToggle={!forceCompact} />
      </aside>

      <DialogPrimitive.Root open={mobileOpen} onOpenChange={(open) => { if (!open) closeMobile(); }}>
        <DialogPrimitive.Portal>
          <DialogPrimitive.Backdrop className="fixed inset-0 z-50 bg-black/50 transition-opacity duration-200 data-[ending-style]:opacity-0 data-[starting-style]:opacity-0 md:hidden" />
          <DialogPrimitive.Viewport className="fixed inset-0 z-50 md:hidden">
            <DialogPrimitive.Popup className="flex h-full w-[min(20rem,88vw)] flex-col border-r bg-sidebar text-sidebar-foreground shadow-2xl outline-none transition-transform duration-200 data-[ending-style]:-translate-x-full data-[starting-style]:-translate-x-full">
              <DialogPrimitive.Title className="sr-only">Primary navigation</DialogPrimitive.Title>
              <DialogPrimitive.Description className="sr-only">Navigate the AgentFIXBPRO workspace.</DialogPrimitive.Description>
              <div className="flex h-16 items-center justify-between border-b px-4">
                <Link href="/" onClick={closeMobile} className="flex min-h-11 items-center gap-2" aria-label="AgentFIXBPRO home">
                  <span className="flex size-8 items-center justify-center rounded-lg bg-primary font-mono text-xs font-bold text-primary-foreground">FX</span>
                  <span className="font-heading text-sm font-semibold">AgentFIXBPRO</span>
                </Link>
                <DialogPrimitive.Close className="inline-flex size-11 items-center justify-center rounded-lg text-muted-foreground hover:bg-muted hover:text-foreground" aria-label="Close navigation">
                  <X className="size-5" />
                </DialogPrimitive.Close>
              </div>
              <div className="min-h-0 flex-1 overflow-y-auto">
                <NavContent onNavigate={closeMobile} />
              </div>
              <SidebarFooter collapsed={false} />
            </DialogPrimitive.Popup>
          </DialogPrimitive.Viewport>
        </DialogPrimitive.Portal>
      </DialogPrimitive.Root>
    </>
  );
}
