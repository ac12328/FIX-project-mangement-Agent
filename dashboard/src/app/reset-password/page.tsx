"use client";

import { Suspense, useEffect, useState, type FormEvent } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { ArrowLeft, ArrowRight, CircleCheck, KeyRound } from "lucide-react";

import { AuthLayout } from "@/components/auth/auth-layout";
import { PasswordField } from "@/components/auth/password-field";
import { Button } from "@/components/ui/button";
import { ApiError, getAuthConfig, resetPassword } from "@/lib/api";

function ResetFallback() {
  return (
    <div className="flex min-h-[calc(100vh-3rem)] items-center justify-center" role="status">
      <span className="size-5 animate-spin rounded-full border-2 border-primary border-t-transparent" />
      <span className="ml-3 text-sm text-muted-foreground">Preparing password reset...</span>
    </div>
  );
}

export default function ResetPasswordPage() {
  return (
    <Suspense fallback={<ResetFallback />}>
      <ResetPasswordPageContent />
    </Suspense>
  );
}

function ResetPasswordPageContent() {
  const searchParams = useSearchParams();
  const [token] = useState(() => searchParams.get("token")?.trim() ?? "");
  const [minimumLength, setMinimumLength] = useState(8);
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState("");
  const [successMessage, setSuccessMessage] = useState("");
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (token) window.history.replaceState(null, "", "/reset-password");
    void getAuthConfig()
      .then((config) => setMinimumLength(Math.max(8, config.password_min_length)))
      .catch(() => undefined);
  }, [token]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    if (!token) {
      setError("This password reset link is missing its token. Request a new link.");
      return;
    }
    if (password.length < minimumLength) {
      setError(`Password must be at least ${minimumLength} characters.`);
      return;
    }
    if (password !== confirmPassword) {
      setError("Passwords do not match.");
      return;
    }

    setSubmitting(true);
    try {
      const result = await resetPassword(token, password);
      setSuccessMessage(result.message);
      setPassword("");
      setConfirmPassword("");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not reset this password.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <AuthLayout
      eyebrow="Choose a new password"
      title={successMessage ? "Password updated" : "Create a new password"}
      description="A successful reset signs out all existing Dashboard sessions for this account."
      footer={(
        <Link href="/login" className="inline-flex min-h-11 items-center gap-2 font-semibold text-primary underline-offset-4 hover:underline">
          <ArrowLeft className="size-4" />
          Back to sign in
        </Link>
      )}
    >
      {successMessage ? (
        <div className="space-y-5">
          <div className="rounded-2xl border border-success/30 bg-success/10 p-5 text-success" role="status">
            <CircleCheck className="size-6" />
            <h2 className="mt-3 font-heading text-lg font-semibold">Password reset complete</h2>
            <p className="mt-1 text-sm leading-6">{successMessage}</p>
          </div>
          <Button size="lg" className="w-full" render={<Link href="/login?reset=1" />}>
            Continue to sign in
            <ArrowRight className="ml-auto size-4" />
          </Button>
        </div>
      ) : (
        <form className="space-y-5" onSubmit={handleSubmit}>
          {!token ? (
            <div className="rounded-xl border border-warning/30 bg-warning/10 px-4 py-3 text-sm leading-6 text-warning" role="alert">
              This page needs a valid password reset token. Request a new email to continue.
            </div>
          ) : null}

          {error ? (
            <p className="rounded-xl border border-destructive/25 bg-destructive/10 px-3 py-3 text-sm text-destructive" role="alert">
              {error}
            </p>
          ) : null}

          <PasswordField
            id="new-password"
            name="password"
            label="New password"
            autoComplete="new-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            minLength={minimumLength}
            helperText={`Use at least ${minimumLength} characters. A long passphrase is recommended.`}
            disabled={submitting || !token}
            required
            autoFocus
          />

          <PasswordField
            id="confirm-password"
            name="confirm_password"
            label="Confirm new password"
            autoComplete="new-password"
            value={confirmPassword}
            onChange={(event) => setConfirmPassword(event.target.value)}
            error={confirmPassword && password !== confirmPassword ? "Passwords do not match." : undefined}
            disabled={submitting || !token}
            required
          />

          <Button
            type="submit"
            size="lg"
            className="w-full"
            disabled={submitting || !token || !password || !confirmPassword}
          >
            {submitting ? (
              <>
                <span className="size-4 animate-spin rounded-full border-2 border-current border-t-transparent" />
                Updating password...
              </>
            ) : (
              <>
                <KeyRound className="size-4" />
                Update password
              </>
            )}
          </Button>

          {!token ? (
            <Button variant="outline" className="w-full" render={<Link href="/forgot-password" />}>
              Request a new reset link
            </Button>
          ) : null}
        </form>
      )}
    </AuthLayout>
  );
}
