import { CheckCircle2 } from "lucide-react";

import {
  diffReviewNotes,
  summarizeNotesSegment,
  type NotesSegment,
} from "@/lib/review-notes";
import type { ReviewChange } from "@/lib/types";

export function NotesChangeComparison({ change }: { change: ReviewChange }) {
  const diff = diffReviewNotes(change.old_value, change.new_value);

  return (
    <article className="overflow-hidden rounded-xl border border-border/80 bg-background">
      <div className="border-b border-border/60 bg-muted/30 px-4 py-2.5">
        <p className="text-sm font-semibold text-foreground">Notes</p>
        <p className="mt-0.5 text-xs text-muted-foreground">
          {diff.mode === "append"
            ? "Existing notes stay as they are. Only the added line is new."
            : "Compare the readable note change, not the raw email identifiers."}
        </p>
      </div>
      <div className="space-y-4 p-4">
        {change.old_value ? (
          <div>
            <p className="text-xs font-bold uppercase tracking-[0.12em] text-muted-foreground">Current notes</p>
            <p className="mt-2 text-sm leading-6 text-foreground/70">{diff.existingPreview || "Not set"}</p>
            {diff.existingCollapsed ? (
              <details className="mt-2">
                <summary className="min-h-11 cursor-pointer text-sm font-medium text-primary underline-offset-4 hover:underline">
                  Show full current notes
                </summary>
                <p className="mt-2 whitespace-pre-wrap break-words text-sm leading-6 text-muted-foreground">
                  {change.old_value}
                </p>
              </details>
            ) : null}
          </div>
        ) : (
          <p className="text-sm italic text-muted-foreground">No current notes</p>
        )}

        {diff.added.length ? (
          <div className="rounded-lg bg-primary/5 p-3 ring-1 ring-inset ring-primary/15">
            <div className="flex items-center gap-1.5 text-xs font-bold uppercase tracking-[0.12em] text-primary">
              <CheckCircle2 className="size-3.5" aria-hidden="true" />
              {diff.mode === "append" ? "Adding" : "Proposed notes"}
            </div>
            <SegmentList segments={diff.added} />
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">No readable note change was found.</p>
        )}
      </div>
    </article>
  );
}

function SegmentList({ segments }: { segments: NotesSegment[] }) {
  return (
    <ul className="mt-2 space-y-2">
      {segments.map((segment, index) => (
        <li key={`${segment.kind}-${index}`} className="text-sm font-medium leading-6 text-foreground">
          {summarizeNotesSegment(segment)}
        </li>
      ))}
    </ul>
  );
}
