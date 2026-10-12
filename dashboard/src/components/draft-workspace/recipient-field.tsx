"use client";

import { useMemo, useState } from "react";
import type { KeyboardEvent } from "react";
import { AlertTriangle, Plus, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import type { WorkspaceContact, WorkspaceRecipient } from "@/lib/draft-workspace-api";
import { cn } from "@/lib/utils";

interface RecipientFieldProps {
  id: string;
  label: string;
  recipients: WorkspaceRecipient[];
  contacts: WorkspaceContact[];
  readOnly?: boolean;
  invalid?: boolean;
  helperText?: string;
  onChange: (recipients: WorkspaceRecipient[]) => void;
}

const EMAIL_RE = /^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$/;
const LABEL_CLASS_NAME =
  "pt-2 text-xs font-semibold uppercase tracking-[0.12em] text-muted-foreground";

export function RecipientField({
  id,
  label,
  recipients,
  contacts,
  readOnly = false,
  invalid = false,
  helperText,
  onChange,
}: RecipientFieldProps) {
  const [input, setInput] = useState("");
  const [inputError, setInputError] = useState<string | null>(null);
  const datalistId = `${id}-contacts`;
  const labelId = `${id}-label`;
  const contactsByEmail = useMemo(
    () => new Map(contacts.map((contact) => [contact.primary_email.toLowerCase(), contact])),
    [contacts],
  );

  const addRecipient = (raw: string) => {
    const token = raw.trim().replace(/[;,]+$/, "");
    if (!token) return;
    const bracketMatch = token.match(/^(.*?)\s*<([^>]+)>$/);
    const email = (bracketMatch?.[2] ?? token).trim();
    const displayName = bracketMatch?.[1]?.trim() || null;
    const canonical = email.toLowerCase();
    if (!EMAIL_RE.test(canonical)) {
      setInputError("Enter a valid email address.");
      return;
    }
    if (recipients.some((item) => item.email.toLowerCase() === canonical)) {
      setInput("");
      setInputError(null);
      return;
    }
    const contact = contactsByEmail.get(canonical);
    onChange([
      ...recipients,
      {
        email,
        display_name: displayName || contact?.display_name || null,
        contact_id: contact?.contact_id || null,
        source: contact ? "project_contact" : "manual",
      },
    ]);
    setInput("");
    setInputError(null);
  };

  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (["Enter", ",", ";"].includes(event.key)) {
      event.preventDefault();
      addRecipient(input);
    } else if (event.key === "Backspace" && !input && recipients.length) {
      onChange(recipients.slice(0, -1));
    }
  };

  return (
    <div className="grid gap-1.5 sm:grid-cols-[4rem_minmax(0,1fr)] sm:items-start">
      {readOnly ? (
        <span id={labelId} className={LABEL_CLASS_NAME}>
          {label}
        </span>
      ) : (
        <label htmlFor={id} className={LABEL_CLASS_NAME}>
          {label}
        </label>
      )}
      <div>
        <div
          role={readOnly ? "group" : undefined}
          aria-labelledby={readOnly ? labelId : undefined}
          className={cn(
            "flex min-h-11 flex-wrap items-center gap-1.5 rounded-lg border bg-background px-2 py-1.5 shadow-xs focus-within:border-primary/60 focus-within:ring-3 focus-within:ring-primary/10",
            invalid && "border-danger/70 ring-3 ring-danger/10",
            readOnly && "bg-muted/30",
          )}
        >
          {recipients.map((recipient) => {
            const external = recipient.source === "manual" && !recipient.contact_id;
            return (
              <span
                key={recipient.email.toLowerCase()}
                className={cn(
                  "inline-flex min-h-7 max-w-full items-center gap-1 rounded-full border bg-muted/60 pl-2.5 text-xs",
                  external && "border-warning/50 bg-warning/10 text-foreground",
                  readOnly && "pr-2.5",
                )}
                title={`${recipient.display_name ? `${recipient.display_name} ` : ""}<${recipient.email}>`}
              >
                {external && (
                  <AlertTriangle
                    className="size-3 text-warning"
                    aria-label="Not linked to a project contact"
                  />
                )}
                <span className="max-w-56 truncate">
                  {recipient.display_name || recipient.email}
                </span>
                {recipient.display_name && (
                  <span className="hidden text-muted-foreground md:inline">{recipient.email}</span>
                )}
                {!readOnly && (
                  <button
                    type="button"
                    className="grid size-7 place-items-center rounded-full text-muted-foreground hover:bg-foreground/10 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                    aria-label={`Remove ${recipient.display_name || recipient.email}`}
                    onClick={() =>
                      onChange(
                        recipients.filter(
                          (item) =>
                            item.email.toLowerCase() !== recipient.email.toLowerCase(),
                        ),
                      )
                    }
                  >
                    <X className="size-3" />
                  </button>
                )}
              </span>
            );
          })}
          {!readOnly && (
            <div className="flex min-w-44 flex-1 items-center">
              <input
                id={id}
                type="email"
                list={datalistId}
                value={input}
                onChange={(event) => {
                  setInput(event.target.value);
                  if (inputError) setInputError(null);
                }}
                onKeyDown={onKeyDown}
                onBlur={() => input.trim() && addRecipient(input)}
                placeholder={recipients.length ? "Add another" : "name@example.com"}
                aria-invalid={Boolean(inputError || invalid)}
                aria-describedby={helperText || inputError ? `${id}-help` : undefined}
                className="min-h-8 min-w-32 flex-1 bg-transparent px-1 text-sm outline-none placeholder:text-muted-foreground"
              />
              {input.trim() && (
                <Button
                  type="button"
                  size="icon-xs"
                  variant="ghost"
                  aria-label={`Add ${input}`}
                  onPointerDown={(event) => event.preventDefault()}
                  onClick={() => addRecipient(input)}
                >
                  <Plus className="size-3.5" />
                </Button>
              )}
            </div>
          )}
          {!readOnly && (
            <datalist id={datalistId}>
              {contacts.map((contact) => (
                <option key={contact.contact_id} value={contact.primary_email}>
                  {contact.display_name}
                </option>
              ))}
            </datalist>
          )}
        </div>
        {(inputError || helperText) && (
          <p
            id={`${id}-help`}
            className={cn(
              "mt-1 text-xs",
              inputError ? "text-danger" : "text-muted-foreground",
            )}
          >
            {inputError || helperText}
          </p>
        )}
      </div>
    </div>
  );
}
