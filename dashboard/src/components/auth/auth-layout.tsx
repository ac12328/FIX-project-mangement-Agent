import type { ReactNode } from "react";
import Link from "next/link";
import { Activity, Route, ShieldCheck, UsersRound } from "lucide-react";

import { cn } from "@/lib/utils";

type AuthLayoutProps = {
  eyebrow: string;
  title: string;
  description: string;
  children: ReactNode;
  footer?: ReactNode;
  className?: string;
};

const workspaceBenefits = [
  {
    icon: Route,
    title: "One operational view",
    description: "Projects, email drafts, approvals, and follow-ups stay connected.",
  },
  {
    icon: UsersRound,
    title: "Role-aware access",
    description: "Viewer, Editor, and Admin permissions protect sensitive actions.",
  },
  {
    icon: Activity,
    title: "Audited sessions",
    description: "Sign-ins and account changes are recorded for administrators.",
  },
];

export function AuthLayout({
  eyebrow,
  title,
  description,
  children,
  footer,
  className,
}: AuthLayoutProps) {
  return (
    <div className="mx-auto grid min-h-[calc(100vh-3rem)] w-full max-w-6xl items-center gap-8 py-6 lg:grid-cols-[1.1fr_0.9fr] lg:py-10">
      <section className="relative hidden min-h-[620px] overflow-hidden rounded-3xl border bg-[radial-gradient(circle_at_top_left,var(--color-primary),transparent_38%),linear-gradient(145deg,var(--color-card),var(--color-muted))] p-10 shadow-xl shadow-black/5 lg:flex lg:flex-col lg:justify-between">
        <div className="absolute -right-20 -top-20 size-72 rounded-full border border-primary/15 bg-primary/5" />
        <div className="absolute -bottom-32 -left-20 size-80 rounded-full border border-primary/10 bg-background/50" />

        <div className="relative">
          <Link href="/login" className="inline-flex min-h-11 items-center gap-3 rounded-xl pr-3 focus-visible:outline-none focus-visible:ring-3 focus-visible:ring-ring/50">
            <span className="flex size-11 items-center justify-center rounded-xl bg-primary text-primary-foreground shadow-lg shadow-primary/20">
              <ShieldCheck className="size-5" />
            </span>
            <span>
              <span className="block font-heading text-lg font-semibold tracking-tight">AgentFIXBPRO</span>
              <span className="block text-xs text-muted-foreground">FIX onboarding workspace</span>
            </span>
          </Link>
        </div>

        <div className="relative max-w-xl space-y-5">
          <div className="inline-flex items-center gap-2 rounded-full border bg-background/70 px-3 py-1.5 text-xs font-semibold text-muted-foreground shadow-sm backdrop-blur">
            <ShieldCheck className="size-4 text-primary" />
            Secure corporate access
          </div>
          <h2 className="font-heading text-4xl font-semibold leading-tight tracking-tight xl:text-5xl">
            Keep every FIX onboarding decision in one trusted workspace.
          </h2>
          <p className="max-w-lg text-base leading-7 text-muted-foreground">
            Sign in with your configured demo email to review work, edit client communications, and manage delivery safely.
          </p>
        </div>

        <div className="relative grid gap-3 sm:grid-cols-3">
          {workspaceBenefits.map((benefit) => {
            const Icon = benefit.icon;
            return (
              <div key={benefit.title} className="rounded-2xl border bg-background/70 p-4 shadow-sm backdrop-blur">
                <Icon className="size-5 text-primary" />
                <p className="mt-3 text-sm font-semibold text-foreground">{benefit.title}</p>
                <p className="mt-1 text-xs leading-5 text-muted-foreground">{benefit.description}</p>
              </div>
            );
          })}
        </div>
      </section>

      <section className={cn("mx-auto w-full max-w-md", className)}>
        <div className="mb-6 lg:hidden">
          <Link href="/login" className="inline-flex min-h-11 items-center gap-3 rounded-xl pr-3 focus-visible:outline-none focus-visible:ring-3 focus-visible:ring-ring/50">
            <span className="flex size-10 items-center justify-center rounded-xl bg-primary text-primary-foreground">
              <ShieldCheck className="size-5" />
            </span>
            <span>
              <span className="block font-heading font-semibold tracking-tight">AgentFIXBPRO</span>
              <span className="block text-xs text-muted-foreground">FIX onboarding workspace</span>
            </span>
          </Link>
        </div>

        <div className="rounded-2xl border bg-card p-6 shadow-xl shadow-black/5 sm:p-8">
          <div className="mb-7">
            <p className="text-xs font-semibold uppercase tracking-[0.16em] text-primary">{eyebrow}</p>
            <h1 className="mt-2 font-heading text-2xl font-semibold tracking-tight sm:text-3xl">{title}</h1>
            <p className="mt-2 text-sm leading-6 text-muted-foreground">{description}</p>
          </div>
          {children}
        </div>

        {footer ? <div className="mt-5 text-center text-sm text-muted-foreground">{footer}</div> : null}
      </section>
    </div>
  );
}
