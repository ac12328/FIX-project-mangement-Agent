export type NotesSegment =
  | { kind: "text"; text: string }
  | { kind: "reconciliation"; hash: string; emailCount: number };

export type NotesDiff = {
  mode: "append" | "replace" | "unchanged";
  existingPreview: string;
  existingCollapsed: boolean;
  added: NotesSegment[];
  removed: NotesSegment[];
};

const PREVIEW_LIMIT = 220;

function stampPattern(): RegExp {
  return /\[email-reconciliation:([^\]]+)\]\s*([\s\S]*?)(?=\s*\[email-reconciliation:|$)/g;
}

export function parseNotesSegments(value: string): NotesSegment[] {
  const text = value.trim();
  if (!text) return [];
  const segments: NotesSegment[] = [];
  let cursor = 0;
  for (const match of text.matchAll(stampPattern())) {
    const index = match.index ?? 0;
    const before = text.slice(cursor, index).trim();
    if (before) segments.push({ kind: "text", text: before });
    const body = match[2] ?? "";
    const emailCount = body.match(/processed_emails:/g)?.length ?? countReconciledMessages(body);
    segments.push({
      kind: "reconciliation",
      hash: match[1] ?? "",
      emailCount,
    });
    cursor = index + match[0].length;
  }
  const rest = text.slice(cursor).trim();
  if (rest) segments.push({ kind: "text", text: rest });
  return segments;
}

function countReconciledMessages(body: string): number {
  const counted = body.match(/\((\d+)\s+messages?\)/i);
  if (counted) return Number.parseInt(counted[1], 10) || 0;
  return 0;
}

export function summarizeNotesSegment(segment: NotesSegment): string {
  if (segment.kind === "text") return segment.text;
  if (segment.emailCount > 0) {
    return `Email history reconciled (${segment.emailCount} email${segment.emailCount === 1 ? "" : "s"}).`;
  }
  return "Email history reconciled.";
}

export function diffReviewNotes(oldValue?: string, newValue?: string): NotesDiff {
  const oldText = (oldValue || "").trim();
  const newText = (newValue || "").trim();
  if (!newText) {
    return {
      mode: "unchanged",
      existingPreview: previewText(oldText),
      existingCollapsed: oldText.length > PREVIEW_LIMIT,
      added: [],
      removed: [],
    };
  }
  if (!oldText) {
    return {
      mode: "replace",
      existingPreview: "",
      existingCollapsed: false,
      added: parseNotesSegments(newText),
      removed: [],
    };
  }
  if (newText === oldText) {
    return {
      mode: "unchanged",
      existingPreview: previewText(oldText),
      existingCollapsed: oldText.length > PREVIEW_LIMIT,
      added: [],
      removed: [],
    };
  }
  if (newText.startsWith(oldText)) {
    return {
      mode: "append",
      existingPreview: previewText(oldText),
      existingCollapsed: oldText.length > PREVIEW_LIMIT,
      added: parseNotesSegments(newText.slice(oldText.length)),
      removed: [],
    };
  }
  return {
    mode: "replace",
    existingPreview: previewText(oldText),
    existingCollapsed: oldText.length > PREVIEW_LIMIT,
    added: parseNotesSegments(newText),
    removed: parseNotesSegments(oldText),
  };
}

function previewText(value: string): string {
  const summarized = parseNotesSegments(value)
    .map((segment) => summarizeNotesSegment(segment))
    .join(" ")
    .replace(/\s+/g, " ")
    .trim();
  if (summarized.length <= PREVIEW_LIMIT) return summarized;
  return `${summarized.slice(0, PREVIEW_LIMIT).trimEnd()}…`;
}
