"use client";

import { useEffect, useId, useRef, useState } from "react";

import type { AssistantSourcePart } from "@/lib/assistant-types";
import { assistantSourceHref, isExternalAssistantSource } from "@/lib/assistant-sources";

function chipLabel(title: string) {
  const trimmed = title.trim();
  if (trimmed.length <= 32) return trimmed;
  return `${trimmed.slice(0, 31).trimEnd()}…`;
}

export function CitationChip({ source }: { source: AssistantSourcePart }) {
  const panelId = useId();
  const rootRef = useRef<HTMLSpanElement>(null);
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState({ top: 0, left: 0 });
  const href = assistantSourceHref(source);
  const external = isExternalAssistantSource(source);
  const label = chipLabel(source.title);

  useEffect(() => {
    if (!open) return;
    function close(event: Event) {
      if (event.type === "keydown" && (event as KeyboardEvent).key !== "Escape") return;
      if (event.type === "pointerdown" && rootRef.current?.contains(event.target as Node)) return;
      setOpen(false);
    }
    document.addEventListener("pointerdown", close);
    document.addEventListener("keydown", close);
    document.addEventListener("scroll", close, true);
    return () => {
      document.removeEventListener("pointerdown", close);
      document.removeEventListener("keydown", close);
      document.removeEventListener("scroll", close, true);
    };
  }, [open]);

  return (
    <span ref={rootRef} className="not-prose relative inline-flex align-baseline">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={panelId}
        aria-label={`Preview source: ${source.title}`}
        onClick={(event) => {
          const rect = event.currentTarget.getBoundingClientRect();
          setPosition({
            top: rect.bottom + 8,
            left: Math.max(8, Math.min(rect.left, window.innerWidth - 296)),
          });
          setOpen((value) => !value);
        }}
        className="mx-0.5 inline-flex max-w-56 min-h-6 items-center truncate rounded-full border border-primary/25 bg-primary/8 px-2 align-baseline text-xs font-medium leading-5 text-primary transition-colors hover:border-primary/50 hover:bg-primary/15 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        {label}
      </button>
      {open ? (
        <span
          id={panelId}
          role="region"
          aria-label={source.title}
          className="fixed z-40 w-72 max-w-[calc(100vw-1rem)] rounded-xl border border-border bg-popover p-3 text-left text-popover-foreground shadow-xl"
          style={{ top: position.top, left: position.left }}
        >
          <span className="block text-sm font-semibold leading-5 text-foreground">{source.title}</span>
          {source.kind ? (
            <span className="mt-1 block text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
              {source.kind.replaceAll("_", " ")}
            </span>
          ) : null}
          {source.snippet ? (
            <span className="mt-2 block text-xs leading-5 text-muted-foreground">{source.snippet}</span>
          ) : null}
          {href ? (
            <a
              href={href}
              className="mt-3 inline-flex min-h-8 items-center text-sm font-medium text-primary underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              target={external ? "_blank" : undefined}
              rel={external ? "noreferrer" : undefined}
            >
              Open source
            </a>
          ) : null}
        </span>
      ) : null}
    </span>
  );
}
