"use client";

import { Suspense, useEffect, useRef, useState, type FormEvent } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { ArrowRight, CircleCheck, MailCheck, RotateCcw } from "lucide-react";

import { AuthLayout } from "@/components/auth/auth-layout";
import { Button } from "@/components/ui/button";
import { ApiError, resendVerification, verifyEmail } from "@/lib/api";

type VerificationState = "missing" | "verifying" | "success" | "error";

function VerifyFallback() {
  return (
    <div className="flex min-h-[calc(100vh-3rem)] items-center justify-center" role="status">
      <span className="size-5 animate-spin rounded-full border-2 border-primary border-t-transparent" />
      <span className="ml-3 text-sm text-muted-foreground">Preparing email verification...</span>
    </div>
  );
}

export default function VerifyEmailPage() {
  return (
    <Suspense fallback={<VerifyFallback />}>
      <VerifyEmailPageContent />
    </Suspense>
  );
}

function VerifyEmailPageContent() {
  const searchParams = useSearchParams();
  const token = searchParams.get("token")?.trim() ?? "";
  const startedToken = useRef<string | null>(null);
  const [state, setState] = useState<VerificationState>(token ? "verifying" : "missing");
  const [message, setMessage] = useState(token ? "Verifying your email address..." : "This page needs a verification token from your email.");
  const [email, setEmail] = useState("");
  const [resending, setResending] = useState(false);
  const [resendMessage, setResendMessage] = useState("");

  useEffect(() => {
    if (!token || startedToken.current === token) return;
    startedToken.current = token;
    window.history.replaceState(null, "", "/verify-email");

    void verifyEmail(token)
      .then((result) => {
        setState("success");
        setMessage(result.message);
      })
      .catch((err) => {
        setState("error");
        setMessage(err instanceof ApiError ? err.message : "Could not verify this email address.");
      });
  }, [token]);

  async function handleResend(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setResending(true);
    setResendMessage("");
    try {
      const result = await resendVerification(email.trim().toLowerCase());
      setResendMessage(result.message);
    } catch (err) {
      setResendMessage(err instanceof ApiError ? err.message : "Could not request another verification email.");
    } finally {
      setResending(false);
    }
  }

  const success = state === "success";

  return (
    <AuthLayout
      eyebrow="Email verification"
      title={success ? "Email verified" : state === "verifying" ? "Verifying your email" : "Verify your account"}
      description={success
        ? "Your Dashboard account is ready for sign in."
        : "Verification confirms that you control your configured demo email address."}
      footer={(
        <p>
          Already verified?{" "}
          <Link href="/login" className="font-semibold text-primary underline-offset-4 hover:underline">Sign in</Link>
        </p>
      )}
    >
      <div className="space-y-5">
        <div
          className={success
            ? "rounded-2xl border border-success/30 bg-success/10 p-5 text-success"
            : state === "error" || state === "missing"
              ? "rounded-2xl border border-warning/30 bg-warning/10 p-5 text-warning"
              : "rounded-2xl border bg-muted/40 p-5 text-foreground"}
          role={state === "error" || state === "missing" ? "alert" : "status"}
          aria-live="polite"
        >
          {state === "verifying" ? (
            <span className="block size-6 animate-spin rounded-full border-2 border-primary border-t-transparent" />
          ) : success ? (
            <CircleCheck className="size-6" />
          ) : (
            <MailCheck className="size-6" />
          )}
          <p className="mt-3 text-sm leading-6">{message}</p>
        </div>

        {success ? (
          <Button size="lg" className="w-full" render={<Link href="/login?verified=1" />}>
            Continue to sign in
            <ArrowRight className="ml-auto size-4" />
          </Button>
        ) : state !== "verifying" ? (
          <form className="space-y-4 rounded-xl border p-4" onSubmit={handleResend}>
            <div>
              <h2 className="font-heading text-base font-semibold">Request another verification email</h2>
              <p className="mt-1 text-xs leading-5 text-muted-foreground">
                Enter the same corporate email used during registration.
              </p>
            </div>
            <div className="space-y-1.5">
              <label htmlFor="resend-email" className="block text-sm font-medium">Corporate email</label>
              <input
                id="resend-email"
                type="email"
                autoComplete="email"
                value={email}
                onChange={(event) => setEmail(event.target.value)}
                placeholder="name@demo.invalid"
                className="h-11 w-full rounded-lg border bg-background px-3 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/30"
                disabled={resending}
                required
              />
            </div>
            {resendMessage ? (
              <p className="rounded-lg bg-muted px-3 py-2 text-xs leading-5 text-muted-foreground" role="status">{resendMessage}</p>
            ) : null}
            <Button type="submit" variant="outline" className="w-full" disabled={resending || !email.trim()}>
              <RotateCcw className="size-4" />
              {resending ? "Sending..." : "Resend verification email"}
            </Button>
          </form>
        ) : null}
      </div>
    </AuthLayout>
  );
}
