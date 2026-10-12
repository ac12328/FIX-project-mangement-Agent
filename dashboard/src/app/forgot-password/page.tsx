"use client";

import { useState, type FormEvent } from "react";
import Link from "next/link";
import { ArrowLeft, CircleCheck, Mail, Send } from "lucide-react";

import { AuthLayout } from "@/components/auth/auth-layout";
import { Button } from "@/components/ui/button";
import { ApiError, forgotPassword } from "@/lib/api";

export default function ForgotPasswordPage() {
  const [email, setEmail] = useState("");
  const [error, setError] = useState("");
  const [successMessage, setSuccessMessage] = useState("");
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    setError("");
    try {
      const result = await forgotPassword(email.trim().toLowerCase());
      setSuccessMessage(result.message);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not request a password reset.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <AuthLayout
      eyebrow="Password recovery"
      title="Reset your password"
      description="Enter your corporate email. For security, the response is the same whether or not an account exists."
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
            <h2 className="mt-3 font-heading text-lg font-semibold">Request received</h2>
            <p className="mt-1 text-sm leading-6">{successMessage}</p>
          </div>
          <p className="text-sm leading-6 text-muted-foreground">
            If an email arrives, open the password reset link before it expires. You can close this page safely.
          </p>
          <Button className="w-full" render={<Link href="/login" />}>Return to sign in</Button>
        </div>
      ) : (
        <form className="space-y-5" onSubmit={handleSubmit}>
          {error ? (
            <p className="rounded-xl border border-destructive/25 bg-destructive/10 px-3 py-3 text-sm text-destructive" role="alert">
              {error}
            </p>
          ) : null}

          <div className="space-y-1.5">
            <label htmlFor="email" className="block text-sm font-medium">Corporate email</label>
            <div className="relative">
              <Mail className="pointer-events-none absolute left-3 top-3.5 size-4 text-muted-foreground" />
              <input
                id="email"
                type="email"
                autoComplete="email"
                value={email}
                onChange={(event) => setEmail(event.target.value)}
                placeholder="name@demo.invalid"
                className="h-11 w-full rounded-lg border bg-background px-3 pl-10 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/30"
                disabled={submitting}
                required
                autoFocus
              />
            </div>
            <p className="text-xs leading-5 text-muted-foreground">Only exact @demo.invalid addresses are accepted.</p>
          </div>

          <Button type="submit" size="lg" className="w-full" disabled={submitting || !email.trim()}>
            {submitting ? (
              <>
                <span className="size-4 animate-spin rounded-full border-2 border-current border-t-transparent" />
                Sending request...
              </>
            ) : (
              <>
                <Send className="size-4" />
                Send reset link
              </>
            )}
          </Button>
        </form>
      )}
    </AuthLayout>
  );
}
