"use client";

import { FormEvent, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ArrowLeft, MailPlus } from "lucide-react";

import { FormField } from "@/components/form-field";
import { PageHeader } from "@/components/page-header";
import { SectionPanel } from "@/components/section-panel";
import { ErrorPanel } from "@/components/state-panel";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { ApiError, createStandaloneDraft } from "@/lib/api";
import { notifyError, notifySuccess } from "@/lib/notifications";

export default function ComposePage() {
  const router = useRouter();
  const [toEmail, setToEmail] = useState("");
  const [subject, setSubject] = useState("");
  const [body, setBody] = useState("");
  const [cc, setCc] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    setError("");
    try {
      const result = await createStandaloneDraft({
        context: body,
        to_email: toEmail,
        subject,
        body,
        cc: cc || undefined,
      });
      notifySuccess("Draft created", "Opening the review workspace.");
      router.push(`/drafts/${result.draft_id}`);
    } catch (err) {
      const message = err instanceof ApiError ? err.message : "Could not create draft.";
      setError(message);
      notifyError(err, message);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="mx-auto max-w-4xl space-y-6">
      <PageHeader
        eyebrow="Work"
        title="Compose email"
        description="Create a standalone draft for review before anything is sent."
        actions={
          <Button variant="outline" render={<Link href="/mail?tab=drafts" />}>
            <ArrowLeft className="size-4" />
            Back to drafts
          </Button>
        }
      />

      {error ? <ErrorPanel description={error} /> : null}

      <SectionPanel
        title="Message details"
        description="Recipient, subject, and message content are required. The draft remains editable after creation."
      >
        <form onSubmit={submit} className="grid gap-5 pt-5">
          <div className="grid gap-5 sm:grid-cols-2">
            <FormField label="To">
              <Input
                type="email"
                required
                autoComplete="email"
                value={toEmail}
                onChange={(event) => setToEmail(event.target.value)}
                placeholder="recipient@example.com"
              />
            </FormField>
            <FormField label="CC" description="Optional; separate multiple recipients with commas.">
              <Input
                value={cc}
                onChange={(event) => setCc(event.target.value)}
                placeholder="copy@example.com"
              />
            </FormField>
          </div>
          <FormField label="Subject">
            <Input
              required
              value={subject}
              onChange={(event) => setSubject(event.target.value)}
              placeholder="Clear, action-oriented subject"
            />
          </FormField>
          <FormField label="Message" description="Enter at least 20 characters so the review workspace has enough context.">
            <Textarea
              required
              rows={14}
              value={body}
              onChange={(event) => setBody(event.target.value)}
              className="min-h-72 resize-y leading-6"
              placeholder="Write the message context and requested action…"
            />
          </FormField>
          <div className="flex flex-col-reverse gap-2 border-t pt-5 sm:flex-row sm:justify-end">
            <Button type="button" variant="outline" render={<Link href="/mail?tab=drafts" />}>Cancel</Button>
            <Button
              type="submit"
              disabled={submitting || !toEmail || !subject || body.trim().length < 20}
            >
              <MailPlus className="size-4" />
              {submitting ? "Creating…" : "Create draft"}
            </Button>
          </div>
        </form>
      </SectionPanel>
    </div>
  );
}
