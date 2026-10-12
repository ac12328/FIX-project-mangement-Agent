"use client";

import { usePathname, useRouter } from "next/navigation";
import { Sparkles } from "lucide-react";
import { cn } from "@/lib/utils";

// Pages that render their own AgentChatDrawer — hide the global FAB there.
const DRAWER_ROUTES = [/^\/support\/[^/]+$/, /^\/projects\/[^/]+$/, /^\/queries(?:\/|$)/, /^\/ask(?:\/|$)/];

export function AskAiFab({ className }: { className?: string }) {
  const router = useRouter();
  const pathname = usePathname();

  if (DRAWER_ROUTES.some((re) => re.test(pathname))) return null;

  return (
    <button
      type="button"
      onClick={() => router.push("/ask")}
      aria-label="Ask"
      className={cn(
        "fixed bottom-6 right-6 z-40 flex items-center gap-2 rounded-full bg-primary px-4 py-2.5",
        "text-sm font-semibold text-primary-foreground shadow-lg",
        "transition-transform hover:-translate-y-0.5 hover:shadow-xl",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
        className,
      )}
    >
      <Sparkles className="size-4" />
      Ask AI
    </button>
  );
}
