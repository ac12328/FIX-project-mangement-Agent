"use client";

import { Suspense, useEffect, useMemo, useState, type FormEvent } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { ArrowLeft, ArrowRight, CircleCheck, Mail, UserRoundPlus } from "lucide-react";

import { AuthLayout } from "@/components/auth/auth-layout";
import { PasswordField } from "@/components/auth/password-field";
import { Button } from "@/components/ui/button";
import {
  ApiError,
  getAuthConfig,
  register,
  resendVerification,
  type AuthConfig,
} from "@/lib/api";

const inputClassName = "h-11 w-full rounded-lg border bg-background px-3 text-sm outline-none transition-shadow placeholder:text-muted-foreground/70 focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/30 disabled:cursor-not-allowed disabled:opacity-50";

function safeRedirectTarget(value: string | null): string {
  return value?.startsWith("/") && !value.startsWith("//") ? value : "/";
}

function RegisterFallback() {
  return (
    <div className="flex min-h-[calc(100vh-3rem)] items-center justify-center" role="status">
      <span className="size-5 animate-spin rounded-full border-2 border-primary border-t-transparent" />
      <span className="ml-3 text-sm text-muted-foreground">Preparing registration...</span>
    </div>
  );
}

export default function RegisterPage() {
  return (
    <Suspense fallback={<RegisterFallback />}>
      <RegisterPageContent />
    </Suspense>
  );
}

function RegisterPageContent() {
  const searchParams = useSearchParams();
  const redirectTo = safeRedirectTarget(searchParams.get("redirect_to"));
  const [config, setConfig] = useState<AuthConfig | null>(null);
  const [displayName, setDisplayName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState("");
  const [successMessage, setSuccessMessage] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [resending, setResending] = useState(false);

  useEffect(() => {
    void getAuthConfig()
      .then(setConfig)
      .catch((err) => setError(err instanceof Error ? err.message : "Could not load registration settings."));
  }, []);

  const allowedDomains = useMemo(
    () => config?.allowed_email_domains?.length ? config.allowed_email_domains : ["demo.invalid"],
    [config],
  );
  const minimumLength = Math.max(8, config?.password_min_length ?? 8);
  const domainLabel = allowedDomains.map((domain) => `@${domain}`).join(" or ");
  const registrationDisabled = config !== null && (
    config.mode !== "local" || !config.local_registration_enabled
  );

  function emailUsesAllowedDomain(value: string): boolean {
    const normalized = value.trim().toLowerCase();
    if (normalized.split("@").length !== 2) return false;
    const domain = normalized.split("@")[1];
    return allowedDomains.some((allowed) => domain === allowed.toLowerCase());
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    setSuccessMessage("");

    if (!emailUsesAllowedDomain(email)) {
      setError(`Use an exact ${domainLabel} corporate email address.`);
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
      const result = await register({
        display_name: displayName.trim(),
        email: email.trim().toLowerCase(),
        password,
      });
      setSuccessMessage(result.message);
      setPassword("");
      setConfirmPassword("");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not submit registration.");
    } finally {
      setSubmitting(false);
    }
  }

  async function handleResend() {
    setResending(true);
    setError("");
    try {
      const result = await resendVerification(email.trim().toLowerCase());
      setSuccessMessage(result.message);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not resend the verification email.");
    } finally {
      setResending(false);
    }
  }

  return (
    <AuthLayout
      eyebrow="Create account"
      title="Register for the Dashboard"
      description={`Registration is limited to exact ${domainLabel} corporate email addresses.`}
      footer={(
        <p>
          Already registered?{" "}
          <Link href={`/login?redirect_to=${encodeURIComponent(redirectTo)}`} className="font-semibold text-primary underline-offset-4 hover:underline">
            Sign in
          </Link>
        </p>
      )}
    >
      {registrationDisabled ? (
        <div className="space-y-5">
          <div className="rounded-xl border border-warning/30 bg-warning/10 px-4 py-3 text-sm leading-6 text-warning" role="alert">
            Self-registration is not available. Contact a Dashboard administrator for access.
          </div>
          <Button variant="outline" className="w-full" render={<Link href="/login" />}>
            <ArrowLeft className="size-4" />
            Back to sign in
          </Button>
        </div>
      ) : successMessage ? (
        <div className="space-y-5">
          <div className="rounded-2xl border border-success/30 bg-success/10 p-5 text-success" role="status">
            <CircleCheck className="size-6" />
            <h2 className="mt-3 font-heading text-lg font-semibold">Registration request received</h2>
            <p className="mt-1 text-sm leading-6">{successMessage}</p>
          </div>
          <p className="text-sm leading-6 text-muted-foreground">
            Open the verification email in the same browser or copy its link here. The verification link expires for security.
          </p>
          <Button className="w-full" render={<Link href={`/login?redirect_to=${encodeURIComponent(redirectTo)}`} />}>
            Continue to sign in
            <ArrowRight className="size-4" />
          </Button>
          <Button type="button" variant="outline" className="w-full" onClick={() => void handleResend()} disabled={resending}>
            <Mail className="size-4" />
            {resending ? "Sending..." : "Resend verification email"}
          </Button>
        </div>
      ) : (
        <form className="space-y-5" onSubmit={handleSubmit}>
          {error ? (
            <p className="rounded-xl border border-destructive/25 bg-destructive/10 px-3 py-3 text-sm text-destructive" role="alert">
              {error}
            </p>
          ) : null}

          <div className="space-y-1.5">
            <label htmlFor="display-name" className="block text-sm font-medium">Display name</label>
            <input
              id="display-name"
              name="display_name"
              autoComplete="name"
              value={displayName}
              onChange={(event) => setDisplayName(event.target.value)}
              placeholder="Your name"
              className={inputClassName}
              maxLength={200}
              disabled={submitting}
              required
              autoFocus
            />
          </div>

          <div className="space-y-1.5">
            <label htmlFor="email" className="block text-sm font-medium">Corporate email</label>
            <input
              id="email"
              name="email"
              type="email"
              autoComplete="username"
              inputMode="email"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              placeholder="name@demo.invalid"
              className={inputClassName}
              disabled={submitting}
              required
            />
            <p className="text-xs leading-5 text-muted-foreground">
              Subdomains and addresses that only end with the company name are not accepted.
            </p>
          </div>

          <PasswordField
            id="password"
            name="password"
            label="Password"
            autoComplete="new-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            minLength={minimumLength}
            helperText={`Use at least ${minimumLength} characters. A long passphrase is recommended.`}
            disabled={submitting}
            required
          />

          <PasswordField
            id="confirm-password"
            name="confirm_password"
            label="Confirm password"
            autoComplete="new-password"
            value={confirmPassword}
            onChange={(event) => setConfirmPassword(event.target.value)}
            error={confirmPassword && password !== confirmPassword ? "Passwords do not match." : undefined}
            disabled={submitting}
            required
          />

          <Button
            type="submit"
            size="lg"
            className="w-full"
            disabled={submitting || !displayName.trim() || !email.trim() || !password || !confirmPassword}
          >
            {submitting ? (
              <>
                <span className="size-4 animate-spin rounded-full border-2 border-current border-t-transparent" />
                Creating account...
              </>
            ) : (
              <>
                <UserRoundPlus className="size-4" />
                Create account
                <ArrowRight className="ml-auto size-4" />
              </>
            )}
          </Button>
        </form>
      )}
    </AuthLayout>
  );
}
