"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import {
  ArrowRight,
  Bell,
  ChevronDown,
  ListTodo,
  LogOut,
  MonitorSmartphone,
  Settings,
  ShieldCheck,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useAuth } from "@/lib/auth-context";
import { getUiOverview, uiV2Keys } from "@/lib/ui-v2-api";
import {
  requestWorkQueueFocus,
  updateWorkQueueLocation,
  WORK_QUEUE_HREF,
} from "@/lib/work-queue-navigation";
import { cn } from "@/lib/utils";

function initialsFor(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return "U";
  return parts.slice(0, 2).map((part) => part[0]?.toUpperCase()).join("");
}

const priorityDotStyles: Record<string, string> = {
  critical: "bg-danger",
  high: "bg-danger",
  medium: "bg-warning",
  low: "bg-muted-foreground",
};

export function SidebarSession({ collapsed = false }: { collapsed?: boolean }) {
  const router = useRouter();
  const { session, hasPermission, signOut, signOutAll } = useAuth();
  const [signingOut, setSigningOut] = useState(false);
  const canReview = hasPermission("business.write");
  const overview = useQuery({
    queryKey: uiV2Keys.overview(),
    queryFn: getUiOverview,
    enabled: canReview,
  });

  const displayName = session?.display_name || session?.email || session?.username || "Dashboard user";
  const role = session?.role || "User";
  const sectionStatus = overview.data?.section_status;
  const queueUnavailable = overview.isError || (Array.isArray(sectionStatus) && sectionStatus.some(
    (section) => section.section === "action_queue" && section.state === "error",
  ));
  const badgeCount = queueUnavailable ? 0 : overview.data?.action_queue_total ?? 0;
  const queueItems = overview.data?.action_queue ?? [];

  function handleOpenWorkQueue() {
    if (window.location.pathname === "/") {
      updateWorkQueueLocation();
      window.setTimeout(requestWorkQueueFocus, 0);
      return;
    }
    router.push(WORK_QUEUE_HREF);
  }

  async function handleSignOut(allDevices = false) {
    if (allDevices && !window.confirm("Sign out every active Dashboard session?")) return;
    setSigningOut(true);
    try {
      if (allDevices) await signOutAll();
      else await signOut();
    } finally {
      router.replace("/login");
      router.refresh();
      setSigningOut(false);
    }
  }

  const workLabel = queueUnavailable
    ? "Open work queue ? data unavailable"
    : badgeCount
      ? `${badgeCount} work items need attention`
      : "Open work queue";

  return (
    <div className={cn("flex flex-col gap-1", collapsed ? "items-center" : undefined)}>
      {canReview ? (
        <DropdownMenu>
          <DropdownMenuTrigger
            render={<Button type="button" variant="ghost" size={collapsed ? "icon" : "default"} className={cn("relative", collapsed ? "w-full" : "w-full justify-start")} />}
            aria-label={workLabel}
          >
            <Bell className="size-4" />
            {collapsed ? null : <span className="truncate">Work</span>}
            {badgeCount > 0 ? (
              <span className={cn(
                "flex h-5 min-w-5 items-center justify-center rounded-md bg-danger px-1 font-mono text-xs font-semibold text-white",
                collapsed ? "absolute right-1 top-1" : "ml-auto",
              )}>
                {badgeCount > 99 ? "99+" : badgeCount}
              </span>
            ) : null}
          </DropdownMenuTrigger>
          <DropdownMenuContent side="top" align="start" className="w-[min(24rem,calc(100vw-1rem))]">
            <DropdownMenuGroup>
              <DropdownMenuLabel className="px-2 py-2">
                <span className="flex items-center justify-between gap-3 text-sm font-semibold text-foreground">
                  <span>My work</span>
                  <span className="font-mono text-xs text-muted-foreground">{overview.isPending ? "Loading?" : queueUnavailable ? "Unavailable" : `${badgeCount} open`}</span>
                </span>
                <span className="mt-1 block text-xs font-normal leading-5 text-muted-foreground">
                  Reviews, drafts, approvals, and follow-ups that need action.
                </span>
              </DropdownMenuLabel>
              <DropdownMenuSeparator />
              {overview.isPending ? (
                <div className="px-2 py-3 text-sm text-muted-foreground" role="status">Loading work items...</div>
              ) : queueUnavailable ? (
                <div className="px-2 py-3 text-sm text-danger">Could not load the work queue.</div>
              ) : queueItems.length ? (
                queueItems.slice(0, 5).map((item) => (
                  <DropdownMenuItem
                    key={`${item.kind}-${item.id}`}
                    className="h-auto items-start py-2.5"
                    onClick={() => router.push(item.action_href)}
                  >
                    <span className={`mt-1.5 size-2 shrink-0 rounded-full ${priorityDotStyles[item.priority] ?? priorityDotStyles.low}`} />
                    <span className="min-w-0 flex-1">
                      <span className="line-clamp-2 block leading-5">{item.title}</span>
                      <span className="mt-1 block truncate text-xs text-muted-foreground">
                        {item.priority} / {item.project_label || "Workspace"}
                      </span>
                    </span>
                  </DropdownMenuItem>
                ))
              ) : (
                <div className="px-2 py-3 text-sm text-muted-foreground">No open work items.</div>
              )}
            </DropdownMenuGroup>
            <DropdownMenuSeparator />
            <DropdownMenuItem onClick={handleOpenWorkQueue}>
              <ListTodo className="size-4" />
              View full action queue
              <ArrowRight className="ml-auto size-4" />
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      ) : null}

      <DropdownMenu>
        <DropdownMenuTrigger
          aria-label={`Open account menu for ${displayName}`}
          className={cn(
            "flex min-h-11 items-center gap-2 rounded-lg text-left transition-colors duration-150 hover:bg-muted focus-visible:outline-none focus-visible:ring-3 focus-visible:ring-ring/50",
            collapsed ? "w-full justify-center px-0" : "w-full px-2",
          )}
        >
          <span className="flex size-8 shrink-0 items-center justify-center rounded-full bg-primary font-mono text-xs font-bold text-primary-foreground">
            {initialsFor(displayName)}
          </span>
          {collapsed ? null : (
            <>
              <span className="min-w-0 flex-1 leading-tight">
                <span className="block truncate text-xs font-semibold">{displayName}</span>
                <span className="block truncate text-xs text-muted-foreground">{role}</span>
              </span>
              <ChevronDown className="size-4 shrink-0 text-muted-foreground" />
            </>
          )}
        </DropdownMenuTrigger>
        <DropdownMenuContent side="top" align="start" className="w-72">
          <DropdownMenuGroup>
            <DropdownMenuLabel className="px-2 py-2">
              <span className="block truncate text-sm font-semibold text-foreground">{displayName}</span>
              <span className="mt-0.5 block truncate text-xs font-normal text-muted-foreground">{session?.email || session?.auth_method || "Dashboard account"}</span>
              <span className="mt-2 inline-flex rounded-md bg-primary/10 px-2 py-1 text-xs font-semibold text-primary">{role}</span>
            </DropdownMenuLabel>
            {hasPermission("personal.write") ? (
              <DropdownMenuItem onClick={() => router.push("/preferences")}>
                <Settings className="size-4" /> Preferences
              </DropdownMenuItem>
            ) : null}
            <DropdownMenuItem onClick={() => router.push("/sessions")}>
              <MonitorSmartphone className="size-4" /> My sessions
            </DropdownMenuItem>
            {hasPermission("admin.users") ? (
              <DropdownMenuItem onClick={() => router.push("/admin")}>
                <ShieldCheck className="size-4" /> Admin console
              </DropdownMenuItem>
            ) : null}
          </DropdownMenuGroup>
          <DropdownMenuSeparator />
          <DropdownMenuItem disabled={signingOut} onClick={() => void handleSignOut(false)}>
            <LogOut className="size-4" /> Sign out
          </DropdownMenuItem>
          <DropdownMenuItem disabled={signingOut} variant="destructive" onClick={() => void handleSignOut(true)}>
            <LogOut className="size-4" /> Sign out all devices
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
}
