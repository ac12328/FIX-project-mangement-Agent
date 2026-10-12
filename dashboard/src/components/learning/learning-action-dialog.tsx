"use client";

import { type FormEvent, useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";

type Fields = Record<string, string>;

export function LearningActionDialog({
  open,
  title,
  description,
  highRisk = false,
  fields = [],
  onClose,
  onConfirm,
}: {
  open: boolean;
  title: string;
  description: string;
  highRisk?: boolean;
  fields?: Array<{ name: string; label: string; required?: boolean; placeholder?: string }>;
  onClose: () => void;
  onConfirm: (reason: string, confirmed: boolean, fields: Fields) => Promise<void>;
}) {
  const [reason, setReason] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [values, setValues] = useState<Fields>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const restore = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (open) return;
    setReason("");
    setConfirmed(false);
    setValues({});
    setError(null);
  }, [open]);

  useEffect(() => {
    if (open) {
      restore.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      return;
    }
    const target = restore.current;
    if (!target) return;
    const frame = window.requestAnimationFrame(() => {
      target.focus();
      restore.current = null;
    });
    return () => window.cancelAnimationFrame(frame);
  }, [open]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await onConfirm(reason.trim(), confirmed, values);
      onClose();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "The action failed. Refresh and try again.");
    } finally {
      setBusy(false);
    }
  }

  const missing = fields.some((field) => field.required && !values[field.name]?.trim());

  return (
    <Dialog open={open} onOpenChange={(nextOpen) => { if (!nextOpen && !busy) onClose(); }}>
      <DialogContent showClose={!busy} aria-label={title}>
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} className="mt-5 space-y-4">
          <label className="block space-y-1.5 text-sm font-medium" htmlFor="learning-action-reason">
            <span>Reason</span>
            <Textarea
              id="learning-action-reason"
              required
              autoFocus
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              className="min-h-24"
            />
          </label>
          {fields.map((field) => (
            <label key={field.name} className="block space-y-1.5 text-sm font-medium">
              <span>{field.label}</span>
              <Input
                required={field.required}
                placeholder={field.placeholder}
                value={values[field.name] ?? ""}
                onChange={(event) => setValues((current) => ({ ...current, [field.name]: event.target.value }))}
              />
            </label>
          ))}
          {highRisk ? (
            <label className="flex min-h-11 items-start gap-3 rounded-xl border border-warning/30 bg-warning/5 p-3 text-sm">
              <input
                type="checkbox"
                checked={confirmed}
                onChange={(event) => setConfirmed(event.target.checked)}
                className="mt-1 size-4 accent-primary"
              />
              <span>I understand this action is destructive or high-risk and confirm the exact displayed resource revision.</span>
            </label>
          ) : null}
          {error ? (
            <p role="alert" className="rounded-xl border border-destructive/20 bg-destructive/10 p-3 text-sm text-destructive">
              {error}
            </p>
          ) : null}
          <DialogFooter>
            <Button type="button" variant="outline" disabled={busy} onClick={onClose}>Cancel</Button>
            <Button type="submit" disabled={busy || !reason.trim() || missing || (highRisk && !confirmed)}>
              {busy ? "Working..." : "Confirm"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
