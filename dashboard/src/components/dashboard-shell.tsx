"use client";

import { useEffect } from "react";
import { usePathname, useRouter } from "next/navigation";
import { ArrowLeft, Menu, ShieldAlert } from "lucide-react";

import { AskAiFab } from "@/components/ask-ai-fab";
import { ErrorState } from "@/components/error-state";
import { Sidebar } from "@/components/sidebar";
import { WorkspaceCommand } from "@/components/workspace-command";
import { Button } from "@/components/ui/button";
import { useAuth } from "@/lib/auth-context";
import { QueryEventInvalidator } from "@/lib/query-events";
import { useSidebar } from "@/lib/sidebar-context";

const PUBLIC_ROUTES = new Set([
  "/login",
  "/register",
  "/verify-email",
  "/forgot-password",
  "/reset-password",
  "/emergency-login",
]);

function permissionForRoute(pathname: string): string | null {
  if (pathname === "/sessions") return null;
  if (pathname === "/admin" || pathname.startsWith("/admin/")) return "admin.users";
  if (pathname === "/llm-config" || pathname.startsWith("/llm-config/")) return "admin.llm_config";
  if (pathname === "/conversation" || pathname.startsWith("/conversation/")) return "admin.agent_context";
  if (pathname === "/learning" || pathname.startsWith("/learning/")) return "admin.learning_governance";
  if (pathname === "/traces" || pathname.startsWith("/traces/")) return "traces.sanitized";
  if (["/assistant", "/queries", "/ask"].some((route) => pathname === route || pathname.startsWith(`${route}/`))) return "assistant.read";
  if (pathname === "/preferences" || pathname.startsWith("/preferences/")) return "personal.write";
  if (pathname.endsWith("/closing")) return "project.close";
  if (["/reviews", "/approvals", "/compose", "/drafts", "/mail", "/support", "/intake"].some(
    (route) => pathname === route || pathname.startsWith(`${route}/`),
  )) return "business.write";
  return "shared.read";
}

export function DashboardShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const { session, loading, error, hasPermission } = useAuth();
  const { openMobile } = useSidebar();
  const publicRoute = PUBLIC_ROUTES.has(pathname);

  useEffect(() => {
    if (publicRoute || loading || session) return;
    const redirectTo = typeof window === "undefined"
      ? pathname
      : `${window.location.pathname}${window.location.search}`;
    router.replace(`/login?redirect_to=${encodeURIComponent(redirectTo)}`);
  }, [loading, pathname, publicRoute, router, session]);

  if (publicRoute) {
    return (
      <main className="min-h-screen bg-background px-4 py-6 text-foreground sm:px-6">
        {children}
      </main>
    );
  }

  if (loading || (!session && !error)) {
    return (
      <main className="flex min-h-screen items-center justify-center bg-background text-foreground">
        <div className="flex items-center gap-3 text-sm text-muted-foreground" role="status">
          <span className="size-4 animate-spin rounded-full border-2 border-primary border-t-transparent" />
          Checking session…
        </div>
      </main>
    );
  }

  if (error) {
    return (
      <main className="flex min-h-screen items-center justify-center bg-background px-4 text-foreground">
        <ErrorState
          title="Could not verify your session"
          description={error}
          actionLabel="Try again"
          onAction={() => window.location.reload()}
        />
      </main>
    );
  }

  if (!session) return null;

  const requiredPermission = permissionForRoute(pathname);
  const allowed = requiredPermission === null || hasPermission(requiredPermission);
  const immersiveWorkspace = pathname === "/ask";

  return (
    <>
      <QueryEventInvalidator enabled={Boolean(session)} />
      <WorkspaceCommand />
      <div className="fixed inset-0 flex overflow-hidden bg-background">
        <Sidebar />
        <main className={immersiveWorkspace
          ? "h-full min-h-0 min-w-0 flex-1 overflow-hidden"
          : "min-h-0 min-w-0 flex-1 overflow-x-hidden overflow-y-auto px-4 pb-24 pt-6 sm:px-6 md:px-8 md:pt-8 md:pb-8"}>
          {allowed ? children : (
            <section className="mx-auto flex min-h-[60vh] max-w-2xl items-center justify-center">
              <div className="w-full rounded-2xl border border-warning/30 bg-warning/5 p-6 sm:p-8">
                <div className="flex size-12 items-center justify-center rounded-2xl bg-background text-warning shadow-sm ring-1 ring-warning/30">
                  <ShieldAlert className="size-5" />
                </div>
                <h1 className="mt-5 text-2xl font-semibold">Access restricted</h1>
                <p className="mt-2 text-sm leading-6 text-muted-foreground">
                  Your {session.role ?? "current"} role does not include the permission required for this page.
                  If your access was recently changed, refresh the page or contact an administrator.
                </p>
                <Button className="mt-5" variant="outline" onClick={() => router.push("/")}>
                  <ArrowLeft className="size-4" />
                  Back to Dashboard
                </Button>
              </div>
            </section>
          )}
        </main>
      </div>
      <Button
        type="button"
        variant="default"
        size="icon"
        onClick={openMobile}
        aria-label="Toggle navigation"
        className="fixed bottom-4 left-4 z-40 shadow-md md:hidden"
      >
        <Menu className="size-5" />
      </Button>
      {hasPermission("assistant.read") ? <AskAiFab /> : null}
    </>
  );
}
