"use client";

import { useId, useMemo, useState } from "react";
import { AlertTriangle, CalendarClock, Clock3, Mail, Paperclip, RefreshCw, Send } from "lucide-react";

import { WorkspaceDialog } from "@/components/draft-workspace/workspace-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import type { PreflightResult, WorkspaceRecipient } from "@/lib/draft-workspace-api";
import { cn } from "@/lib/utils";

interface FinalPreviewDialogProps {
  open: boolean;
  preflight: PreflightResult | null;
  busy?: boolean;
  retryOnly?: boolean;
  onClose: () => void;
  onSendNow: (confirmedWarnings: string[]) => Promise<void>;
  onSendLater: (confirmedWarnings: string[], scheduledFor: string) => Promise<void>;
}

function recipientLabel(recipient: WorkspaceRecipient): string {
  return recipient.display_name
    ? `${recipient.display_name} <${recipient.email}>`
    : recipient.email;
}

function RecipientRow({ label, recipients }: { label: string; recipients: WorkspaceRecipient[] }) {
  return (
    <div className="grid gap-1 sm:grid-cols-[4rem_minmax(0,1fr)]">
      <dt className="text-xs font-semibold uppercase tracking-[0.1em] text-muted-foreground">
        {label}
      </dt>
      <dd className="flex flex-wrap gap-1.5">
        {recipients.length ? (
          recipients.map((recipient, index) => (
            <Badge
              key={`${label}:${recipient.email.toLowerCase()}:${index}`}
              variant="outline"
              className="h-auto max-w-full whitespace-normal py-1 text-left font-normal"
            >
              {recipientLabel(recipient)}
            </Badge>
          ))
        ) : (
          <span className="text-sm text-muted-foreground">None</span>
        )}
      </dd>
    </div>
  );
}

function defaultScheduleValue(): string {
  const date = new Date(Date.now() + 60 * 60 * 1000);
  date.setMinutes(Math.ceil(date.getMinutes() / 5) * 5, 0, 0);
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 16);
}

type FinalPreviewDialogContentProps = Omit<
  FinalPreviewDialogProps,
  "open" | "preflight"
> & {
  preflight: PreflightResult;
};

export function FinalPreviewDialog(props: FinalPreviewDialogProps) {
  const { open, preflight, ...contentProps } = props;
  if (!open || !preflight) return null;

  return (
    <FinalPreviewDialogContent
      key={preflight.snapshot_hash}
      preflight={preflight}
      {...contentProps}
    />
  );
}

function FinalPreviewDialogContent({
  preflight,
  busy = false,
  retryOnly = false,
  onClose,
  onSendNow,
  onSendLater,
}: FinalPreviewDialogContentProps) {
  const [mode, setMode] = useState<"now" | "later">("now");
  const [scheduledFor, setScheduledFor] = useState(defaultScheduleValue);
  const [confirmed, setConfirmed] = useState<Set<string>>(new Set());
  const [timeZone] = useState(
    () => Intl.DateTimeFormat().resolvedOptions().timeZone || "local time",
  );
  const deliveryModeName = useId();
  const scheduleInputId = useId();
  const scheduleHelpId = useId();
  const scheduleErrorId = useId();
  const bodyHeadingId = useId();
  const attachmentsHeadingId = useId();
  const warningCodes = useMemo(
    () => preflight.warnings.map((issue) => issue.code),
    [preflight],
  );
  const allWarningsConfirmed = warningCodes.every((code) => confirmed.has(code));

  const preview = preflight.preview;
  const scheduledDate = scheduledFor ? new Date(scheduledFor) : null;
  const invalidSchedule =
    mode === "later" &&
    (!scheduledDate || Number.isNaN(scheduledDate.valueOf()) || scheduledDate <= new Date());
  const actionLabel = busy
    ? retryOnly
      ? "Retrying delivery…"
      : "Creating outbox…"
    : retryOnly
      ? "Retry delivery"
      : mode === "now"
        ? "Send with 30s undo"
        : "Schedule delivery";

  return (
    <WorkspaceDialog
      open
      title="Final delivery preview"
      description={
        retryOnly
          ? "Review the immutable outbox snapshot before retrying delivery."
          : "This is the exact sanitized, revision-bound message that will be placed in the durable outbox."
      }
      onClose={busy ? () => undefined : onClose}
      footer={
        <>
          <Button type="button" variant="outline" disabled={busy} onClick={onClose}>
            Back to draft
          </Button>
          <Button
            type="button"
            disabled={busy || !preflight.ok || !allWarningsConfirmed || invalidSchedule}
            onClick={() =>
              retryOnly || mode === "now"
                ? void onSendNow(Array.from(confirmed))
                : void onSendLater(Array.from(confirmed), new Date(scheduledFor).toISOString())
            }
          >
            {retryOnly ? (
              <RefreshCw className="size-4" />
            ) : mode === "now" ? (
              <Send className="size-4" />
            ) : (
              <CalendarClock className="size-4" />
            )}
            {actionLabel}
          </Button>
        </>
      }
    >
      <div className="space-y-5">
        {retryOnly ? (
          <div className="flex min-h-11 items-center gap-2 rounded-xl border border-warning/30 bg-warning/8 px-3 text-sm">
            <RefreshCw className="size-4 text-warning" />
            Retry uses the immutable outbox snapshot shown below.
          </div>
        ) : (
          <fieldset
            className="grid grid-cols-2 gap-2 rounded-xl bg-muted/45 p-1"
            disabled={busy}
          >
            <legend className="sr-only">Delivery time</legend>
            <label>
              <input
                type="radio"
                name={deliveryModeName}
                value="now"
                checked={mode === "now"}
                onChange={() => setMode("now")}
                className="peer sr-only"
                autoFocus
              />
              <span
                className={cn(
                  "flex min-h-11 cursor-pointer items-center justify-center gap-2 rounded-lg px-3 text-sm peer-focus-visible:outline-none peer-focus-visible:ring-2 peer-focus-visible:ring-ring peer-focus-visible:ring-offset-2 peer-disabled:cursor-not-allowed peer-disabled:opacity-60",
                  mode === "now"
                    ? "bg-background font-medium shadow-sm"
                    : "text-muted-foreground",
                )}
              >
                <Mail className="size-4" /> Send now
              </span>
            </label>
            <label>
              <input
                type="radio"
                name={deliveryModeName}
                value="later"
                checked={mode === "later"}
                onChange={() => setMode("later")}
                className="peer sr-only"
              />
              <span
                className={cn(
                  "flex min-h-11 cursor-pointer items-center justify-center gap-2 rounded-lg px-3 text-sm peer-focus-visible:outline-none peer-focus-visible:ring-2 peer-focus-visible:ring-ring peer-focus-visible:ring-offset-2 peer-disabled:cursor-not-allowed peer-disabled:opacity-60",
                  mode === "later"
                    ? "bg-background font-medium shadow-sm"
                    : "text-muted-foreground",
                )}
              >
                <Clock3 className="size-4" /> Send later
              </span>
            </label>
          </fieldset>
        )}

        {!retryOnly && mode === "later" && (
          <div className="rounded-xl border bg-background p-4">
            <label htmlFor={scheduleInputId} className="text-sm font-medium">
              Delivery time
            </label>
            <input
              id={scheduleInputId}
              type="datetime-local"
              value={scheduledFor}
              onChange={(event) => setScheduledFor(event.target.value)}
              className="mt-2 min-h-11 w-full rounded-lg border bg-background px-3 text-sm outline-none focus:border-primary/60 focus:ring-3 focus:ring-primary/10"
              aria-invalid={invalidSchedule}
              aria-describedby={
                invalidSchedule ? `${scheduleHelpId} ${scheduleErrorId}` : scheduleHelpId
              }
            />
            <p id={scheduleHelpId} className="mt-1 text-xs text-muted-foreground">
              Your timezone: {timeZone}. The server stores this time in UTC and re-runs all
              checks before delivery.
            </p>
            {invalidSchedule && (
              <p id={scheduleErrorId} className="mt-1 text-xs text-danger" role="alert">
                Choose a future delivery time.
              </p>
            )}
          </div>
        )}

        {preflight.blockers.length > 0 && (
          <div
            className="rounded-xl border border-danger/30 bg-danger/8 p-4 text-sm text-danger"
            role="alert"
          >
            This snapshot has {preflight.blockers.length} blocker(s). Return to the draft and
            resolve them before sending.
          </div>
        )}

        <section className="rounded-xl border bg-background p-4" aria-label="Message envelope">
          <dl className="space-y-3">
            <div className="grid gap-1 sm:grid-cols-[4rem_minmax(0,1fr)]">
              <dt className="text-xs font-semibold uppercase tracking-[0.1em] text-muted-foreground">
                From
              </dt>
              <dd className="text-sm">
                {preview.from
                  ? `${preview.from.display_name} <${preview.from.email_address}>`
                  : "Not selected"}
              </dd>
            </div>
            <RecipientRow label="To" recipients={preview.to} />
            <RecipientRow label="CC" recipients={preview.cc} />
            <RecipientRow label="BCC" recipients={preview.bcc} />
            <div className="grid gap-1 sm:grid-cols-[4rem_minmax(0,1fr)]">
              <dt className="text-xs font-semibold uppercase tracking-[0.1em] text-muted-foreground">
                Subject
              </dt>
              <dd className="break-words text-sm font-medium">
                {preview.subject || "(No subject)"}
              </dd>
            </div>
            <div className="grid gap-1 sm:grid-cols-[4rem_minmax(0,1fr)]">
              <dt className="text-xs font-semibold uppercase tracking-[0.1em] text-muted-foreground">
                Mode
              </dt>
              <dd className="text-sm capitalize">
                {preview.delivery_mode === "reply"
                  ? "Reply in confirmed customer thread"
                  : preview.delivery_mode === "new"
                    ? "New email"
                    : "Unresolved"}
              </dd>
            </div>
          </dl>
        </section>

        <section aria-labelledby={bodyHeadingId}>
          <h3
            id={bodyHeadingId}
            className="mb-2 text-xs font-semibold uppercase tracking-[0.12em] text-muted-foreground"
          >
            Sanitized body
          </h3>
          <div
            className="prose prose-sm max-w-none rounded-xl border bg-white p-5 text-slate-900"
            dangerouslySetInnerHTML={{ __html: preview.body_html }}
          />
        </section>

        <section aria-labelledby={attachmentsHeadingId}>
          <h3
            id={attachmentsHeadingId}
            className="mb-2 flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.12em] text-muted-foreground"
          >
            <Paperclip className="size-3.5" /> Attachments
          </h3>
          {preview.attachments.length ? (
            <ul className="space-y-1 text-sm">
              {preview.attachments.map((item, index) => (
                <li
                  key={`${item.filename}:${item.sha256 ?? index}`}
                  className="rounded-lg border px-3 py-2"
                >
                  {item.filename}
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-sm text-muted-foreground">No attachments.</p>
          )}
        </section>

        {preflight.warnings.length > 0 && (
          <fieldset
            className="space-y-2 rounded-xl border border-warning/35 bg-warning/8 p-4"
            disabled={busy}
          >
            <legend className="px-1 text-sm font-semibold">Confirm each warning</legend>
            {preflight.warnings.map((warning, index) => (
              <label
                key={`${warning.code}:${warning.field}:${index}`}
                className={cn(
                  "flex cursor-pointer items-start gap-3 rounded-lg bg-background/70 p-3 text-sm",
                  busy && "cursor-not-allowed opacity-60",
                )}
              >
                <input
                  type="checkbox"
                  className="mt-0.5 size-4 accent-primary"
                  checked={confirmed.has(warning.code)}
                  onChange={(event) =>
                    setConfirmed((current) => {
                      const next = new Set(current);
                      if (event.target.checked) next.add(warning.code);
                      else next.delete(warning.code);
                      return next;
                    })
                  }
                />
                <AlertTriangle className="mt-0.5 size-4 shrink-0 text-warning" />
                <span>{warning.message}</span>
              </label>
            ))}
          </fieldset>
        )}
      </div>
    </WorkspaceDialog>
  );
}
