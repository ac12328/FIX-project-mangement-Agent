"use client";

import { type FormEvent, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ArrowLeft, KeyRound, ShieldAlert } from "lucide-react";

import { AuthLayout } from "@/components/auth/auth-layout";
import { PasswordField } from "@/components/auth/password-field";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ApiError, emergencyLogin } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";

function safeRedirectTarget(value: string | null): string {
  return value?.startsWith("/") && !value.startsWith("//") ? value : "/";
}

export default function EmergencyLoginPage() {
  const router = useRouter();
  const { session, loading, refreshSession } = useAuth();
  const [redirectTo, setRedirectTo] = useState("/");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    setRedirectTo(safeRedirectTarget(params.get("redirect_to")));
  }, []);

  useEffect(() => {
    if (!loading && session) router.replace(redirectTo);
  }, [loading, redirectTo, router, session]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    setError("");
    try {
      await emergencyLogin({ username: username.trim(), password });
      await refreshSession();
      router.replace(redirectTo);
      router.refresh();
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : "Could not complete emergency sign in.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <AuthLayout
      eyebrow="Emergency access"
      title="Administrator fallback"
      description="Use only when standard Dashboard sign-in is unavailable. Every attempt is rate-limited and audited."
      footer={
        <Link
          href={`/login?redirect_to=${encodeURIComponent(redirectTo)}`}
          className="inline-flex min-h-11 items-center gap-2 font-semibold text-primary underline-offset-4 hover:underline"
        >
          <ArrowLeft className="size-4" />
          Back to standard sign in
        </Link>
      }
    >
      <div className="mb-5 flex items-start gap-3 rounded-xl border border-warning/30 bg-warning/10 p-3 text-sm">
        <ShieldAlert className="mt-0.5 size-5 shrink-0 text-warning" aria-hidden="true" />
        <p className="leading-6 text-muted-foreground">
          This fallback bypasses the normal corporate identity flow. Use an approved emergency credential only during an active access incident.
        </p>
      </div>

      <form className="space-y-5" onSubmit={handleSubmit}>
        <label className="block space-y-1.5 text-sm font-medium" htmlFor="emergency-username">
          <span>Username</span>
          <Input
            id="emergency-username"
            autoComplete="username"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            required
          />
        </label>

        <PasswordField
          id="emergency-password"
          label="Password"
          autoComplete="current-password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          required
        />

        {error ? (
          <p className="rounded-xl border border-destructive/20 bg-destructive/10 px-3 py-3 text-sm text-destructive" role="alert">
            {error}
          </p>
        ) : null}

        <Button type="submit" className="w-full" disabled={submitting || !username.trim() || !password}>
          <KeyRound className="size-4" />
          {submitting ? "Verifying..." : "Sign in with emergency access"}
        </Button>
      </form>
    </AuthLayout>
  );
}
