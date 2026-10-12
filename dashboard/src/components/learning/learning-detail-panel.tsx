"use client";

import { ReactNode, useEffect, useId, useRef } from "react";
import { X } from "lucide-react";
import { Button } from "@/components/ui/button";

const FOCUSABLE = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "details > summary",
  "[tabindex]:not([tabindex='-1'])",
].join(",");

export function LearningDetailPanel({ title, description, children, onClose }: {
  title: string;
  description?: string;
  children: ReactNode;
  onClose: () => void;
}) {
  const panel = useRef<HTMLElement>(null);
  const restore = useRef<HTMLElement | null>(null);
  const titleId = useId();
  const descriptionId = useId();

  useEffect(() => {
    restore.current = document.activeElement as HTMLElement | null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    window.requestAnimationFrame(() => panel.current?.focus());

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key !== "Tab" || !panel.current) return;
      const controls = [...panel.current.querySelectorAll<HTMLElement>(FOCUSABLE)]
        .filter((item) => !item.hasAttribute("disabled") && item.getAttribute("aria-hidden") !== "true");
      if (!controls.length) {
        event.preventDefault();
        panel.current.focus();
        return;
      }
      const first = controls[0];
      const last = controls[controls.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };

    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousOverflow;
      restore.current?.focus();
    };
  }, [onClose]);

  return <div className="fixed inset-0 z-40">
    <button type="button" tabIndex={-1} aria-hidden="true" className="absolute inset-0 cursor-default bg-foreground/50" onClick={onClose} />
    <aside
      ref={panel}
      role="dialog"
      aria-modal="true"
      aria-labelledby={titleId}
      aria-describedby={description ? descriptionId : undefined}
      tabIndex={-1}
      className="absolute inset-y-0 right-0 w-full overflow-y-auto border-l bg-background text-foreground shadow-2xl outline-none sm:max-w-2xl"
    >
      <header className="sticky top-0 z-10 flex items-start justify-between gap-4 border-b bg-background/95 px-4 py-4 backdrop-blur sm:px-6">
        <div className="min-w-0">
          <h2 id={titleId} className="break-words text-lg font-semibold [overflow-wrap:anywhere]">{title}</h2>
          {description && <p id={descriptionId} className="mt-1 break-words text-sm leading-6 text-muted-foreground [overflow-wrap:anywhere]">{description}</p>}
        </div>
        <Button size="icon-sm" variant="outline" onClick={onClose} aria-label="Close detail panel">
          <X aria-hidden="true" />
        </Button>
      </header>
      <div className="min-w-0 space-y-4 px-4 py-5 sm:px-6 [&_pre]:max-w-full [&_pre]:whitespace-pre-wrap [&_pre]:break-words [&_pre]:[overflow-wrap:anywhere]">{children}</div>
    </aside>
  </div>;
}

export function DetailSection({ title, value }: { title: string; value: unknown }) {
  return <section className="min-w-0 rounded-xl border bg-card p-4">
    <h3 className="font-medium">{title}</h3>
    <pre className="mt-2 text-xs leading-5 text-muted-foreground">{JSON.stringify(value, null, 2)}</pre>
  </section>;
}
