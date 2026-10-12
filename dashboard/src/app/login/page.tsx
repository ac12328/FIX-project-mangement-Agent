"use client";

import { Suspense, useEffect, useState, type FormEvent } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { ArrowRight, CircleCheck, KeyRound, LogIn, Mail } from "lucide-react";

import { AuthLayout } from "@/components/auth/auth-layout";
import { PasswordField } from "@/components/auth/password-field";
import { Button } from "@/components/ui/button";
import { ApiError, getAuthConfig, login, type AuthConfig } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";

const inputClassName = "h-11 w-full rounded-lg border bg-background px-3 text-sm outline-none transition-shadow placeholder:text-muted-foreground/70 focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/30 disabled:cursor-not-allowed disabled:opacity-50";

function safeRedirectTarget(value: string | null): string {
  return value?.startsWith("/") && !value.startsWith("//") ? value : "/";
}

function LoginPageFallback() {
  return (
    <div className="flex min-h-[calc(100vh-3rem)] items-center justify-center" role="status">
      <span className="size-5 animate-spin rounded-full border-2 border-primary border-t-transparent" />
      <span className="ml-3 text-sm text-muted-foreground">Preparing sign in...</span>
    </div>
  );
}

export default function LoginPage() {
  return (
    <Suspense fallback={<LoginPageFallback />}>
      <LoginPageContent />
    </Suspense>
  );
}

function LoginPageContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { session, loading, refreshSession } = useAuth();
  const [authConfig, setAuthConfig] = useState<AuthConfig | null>(null);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState(searchParams.get("error")?.trim() ?? "");
  const [submitting, setSubmitting] = useState(false);
  const redirectTo = safeRedirectTarget(searchParams.get("redirect_to"));
  const statusMessage = searchParams.get("verified") === "1"
    ? "Email verified. You can sign in now."
    : searchParams.get("reset") === "1"
      ? "Password updated. Sign in with your new password."
      : "";

  useEffect(() => {
    void getAuthConfig()
      .then(setAuthConfig)
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    if (!loading && session) router.replace(redirectTo);
  }, [loading, redirectTo, router, session]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    setError("");
    try {
      await login(email.trim().toLowerCase(), password);
      await refreshSession();
      router.replace(redirectTo);
      router.refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not sign in. Please try again.");
    } finally {
      setSubmitting(false);
    }
  }

  const registrationAvailable = authConfig?.local_registration_enabled !== false;
  const domainLabel = authConfig?.allowed_email_domains?.length
    ? authConfig.allowed_email_domains.map((domain) => `@${domain}`).join(" or ")
    : "@demo.invalid";

  return (
    <AuthLayout
      eyebrow="Dashboard sign in"
      title="Welcome back"
      description="Use your configured demo email and Dashboard password to continue."
      footer={registrationAvailable ? (
        <p>
          Need an account?{" "}
          <Link href={`/register?redirect_to=${encodeURIComponent(redirectTo)}`} className="font-semibold text-primary underline-offset-4 hover:underline">
            Register with your work email
          </Link>
        </p>
      ) : (
        <p>Account registration is currently managed by an administrator.</p>
      )}
    >
      <form className="space-y-5" onSubmit={handleSubmit}>
        {statusMessage ? (
          <div className="flex items-start gap-2 rounded-xl border border-success/30 bg-success/10 px-3 py-3 text-sm text-success" role="status">
            <CircleCheck className="mt-0.5 size-4 shrink-0" />
            <span>{statusMessage}</span>
          </div>
        ) : null}

        {error ? (
          <div className="rounded-xl border border-destructive/25 bg-destructive/10 px-3 py-3 text-sm text-destructive" role="alert">
            <p>{error}</p>
            {error.toLowerCase().includes("verify") ? (
              <Link href="/verify-email" className="mt-2 inline-flex font-semibold underline underline-offset-4">
                Resend verification email
              </Link>
            ) : null}
          </div>
        ) : null}

        <div className="space-y-1.5">
          <label htmlFor="email" className="block text-sm font-medium">Corporate email</label>
          <div className="relative">
            <Mail className="pointer-events-none absolute left-3 top-3.5 size-4 text-muted-foreground" />
            <input
              id="email"
              name="email"
              type="email"
              autoComplete="username"
              inputMode="email"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              placeholder="name@demo.invalid"
              className={`${inputClassName} pl-10`}
              disabled={submitting}
              required
              autoFocus
            />
          </div>
          <p className="text-xs leading-5 text-muted-foreground">Only exact {domainLabel} addresses are accepted.</p>
        </div>

        <div className="space-y-2">
          <PasswordField
            id="password"
            name="password"
            label="Password"
            autoComplete="current-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            disabled={submitting}
            required
          />
          <div className="flex justify-end">
            <Link href="/forgot-password" className="inline-flex min-h-11 items-center text-sm font-semibold text-primary underline-offset-4 hover:underline">
              Forgot password?
            </Link>
          </div>
        </div>

        <Button type="submit" size="lg" className="w-full" disabled={submitting || !email.trim() || !password}>
          {submitting ? (
            <>
              <span className="size-4 animate-spin rounded-full border-2 border-current border-t-transparent" />
              Signing in...
            </>
          ) : (
            <>
              <LogIn className="size-4" />
              Sign in
              <ArrowRight className="ml-auto size-4" />
            </>
          )}
        </Button>

        <div className="flex items-center gap-3 text-xs uppercase tracking-[0.14em] text-muted-foreground/70">
          <span className="h-px flex-1 bg-border" />
          Administrator recovery
          <span className="h-px flex-1 bg-border" />
        </div>

        <Button
          type="button"
          variant="outline"
          className="w-full"
          render={<Link href={`/emergency-login?redirect_to=${encodeURIComponent(redirectTo)}`} />}
        >
          <KeyRound className="size-4" />
          Emergency administrator access
        </Button>
      </form>
    </AuthLayout>
  );
}
