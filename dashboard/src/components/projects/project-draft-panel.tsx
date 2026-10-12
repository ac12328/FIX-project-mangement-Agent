"use client";

interface ProjectDraftPanelProps {
  ballWith: string;
  draftText: string;
  draftSubject: string;
  loading: boolean;
  onDraftChange: (text: string) => void;
  onSubjectChange: (text: string) => void;
  onRegenerate: () => void;
  onReviewSend: () => void;
  creatingDraft: boolean;
}

export default function ProjectDraftPanel({
  ballWith,
  draftText,
  draftSubject,
  loading,
  onDraftChange,
  onSubjectChange,
  onRegenerate,
  onReviewSend,
  creatingDraft,
}: ProjectDraftPanelProps) {
  const normalised = ballWith.toLowerCase();

  if (normalised === "resolved" || normalised === "completed" || normalised === "archived") {
    return (
      <div className="rounded-lg border border-success/30 bg-success/10 p-4">
        <p className="text-sm font-semibold text-success">Project Complete</p>
        <p className="mt-1 text-xs text-success/80">
          This project has been closed. No further emails required.
        </p>
      </div>
    );
  }

  if (normalised === "internal" || normalised === "shared") {
    return (
      <div className="rounded-lg border border-border bg-muted/30 p-4">
        <p className="text-sm font-semibold text-foreground">Awaiting Internal Team</p>
        <p className="mt-1 text-xs text-muted-foreground">
          Ball is with the internal team. No client email needed right now.
        </p>
      </div>
    );
  }

  // Default: External — show draft
  return (
    <div className="rounded-lg border border-primary/20 bg-primary/5 p-4">
      <div className="mb-3 flex items-center justify-between">
        <p className="text-sm font-semibold text-primary">AI Draft Email</p>
        <button
          onClick={onRegenerate}
          disabled={loading}
          className="rounded px-2 py-1 text-xs text-primary bg-primary/10 hover:bg-primary/20 disabled:opacity-50 transition-colors"
        >
          {loading ? "..." : "Regenerate"}
        </button>
      </div>
      <div className="mb-2">
        <label className="text-xs font-medium text-muted-foreground">Subject</label>
        <input
          type="text"
          className="mt-1 w-full rounded border border-border bg-background px-2 py-1.5 text-xs text-foreground focus:outline-none focus:ring-1 focus:ring-primary/40"
          value={draftSubject}
          onChange={(e) => onSubjectChange(e.target.value)}
          placeholder={loading ? "Generating..." : "Subject line"}
          disabled={loading}
        />
      </div>
      <textarea
        className="w-full rounded border border-border bg-background p-2 text-xs text-foreground min-h-[280px] resize-y focus:outline-none focus:ring-1 focus:ring-primary/40"
        value={draftText}
        onChange={(e) => onDraftChange(e.target.value)}
        placeholder={loading ? "Generating draft..." : "Draft will appear here"}
        disabled={loading}
      />
      <div className="mt-2 flex gap-2">
        <button
          onClick={onReviewSend}
          disabled={!draftText.trim() || creatingDraft || loading}
          className="flex-1 rounded bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50 transition-colors"
        >
          {creatingDraft ? "Creating..." : "✉ Review & Send"}
        </button>
        <button
          onClick={() => { onDraftChange(""); onSubjectChange(""); }}
          className="rounded bg-muted px-3 py-1.5 text-xs text-muted-foreground hover:bg-muted/80 transition-colors"
        >
          Clear
        </button>
      </div>
    </div>
  );
}
