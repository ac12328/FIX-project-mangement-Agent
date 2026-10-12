import type { ReviewAssessment, ReviewChange, ReviewCitation } from "./types";

const MAX_CITATIONS = 12;
const MAX_CRITERIA = 30;
const MAX_UNCERTAINTIES = 20;
const MAX_CONFIDENCE_DIMENSIONS = 12;
const NOTES_MAX_LENGTH = 12_000;
const REASONING_BLOCK_RE = /<(think|analysis|reasoning)\b[^>]*>[\s\S]*?<\/\1\s*>/gi;
const ESCAPED_REASONING_BLOCK_RE = /(?:&lt;|\\u003c)(think|analysis|reasoning)(?:&gt;|\\u003e)[\s\S]*?(?:&lt;\/|\\u003c\/)\1(?:&gt;|\\u003e)/gi;
const REASONING_OPEN_RE = /<(?:think|analysis|reasoning)\b[^>]*>|(?:&lt;|\\u003c)(?:think|analysis|reasoning)(?:&gt;|\\u003e)/i;
const SUSPICIOUS_PAYLOAD_RE = /\b(?:tool_calls?|function_call|arguments)\b|^\s*[\[{][\s\S]*[\]}]\s*$/i;

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

export type SafeReviewHeader = {
  projectLabel: string;
  clientName?: string;
  taskType?: string;
  riskLevel?: string;
};

export function safeReviewText(value: unknown, maxLength: number): string | undefined {
  if (typeof value !== "string") return undefined;
  let text = value
    .replace(ESCAPED_REASONING_BLOCK_RE, "")
    .replace(REASONING_BLOCK_RE, "");
  const unclosed = text.search(REASONING_OPEN_RE);
  if (unclosed >= 0) text = text.slice(0, unclosed);
  text = text.replace(/\s+/g, " ").trim();
  if (!text || SUSPICIOUS_PAYLOAD_RE.test(text)) return undefined;
  return text.slice(0, maxLength);
}

export function safeReviewNotes(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined;
  let text = value
    .replace(ESCAPED_REASONING_BLOCK_RE, "")
    .replace(REASONING_BLOCK_RE, "");
  const unclosed = text.search(REASONING_OPEN_RE);
  if (unclosed >= 0) text = text.slice(0, unclosed);
  text = text.replace(/\r\n/g, "\n").trim();
  if (!text || SUSPICIOUS_PAYLOAD_RE.test(text)) return undefined;
  return text.slice(0, NOTES_MAX_LENGTH);
}

export function sanitizeReviewHeader(value: {
  project_id: unknown;
  client_name: unknown;
  task_type: unknown;
  risk_level: unknown;
}): SafeReviewHeader {
  return {
    projectLabel: safeReviewText(value.project_id, 80) ?? "Project",
    clientName: safeReviewText(value.client_name, 160),
    taskType: safeReviewText(value.task_type, 80),
    riskLevel: safeReviewText(value.risk_level, 32),
  };
}

function parseCitation(value: unknown): ReviewCitation | null {
  if (!isRecord(value)) return null;
  const quote = safeReviewText(value.quote, 500);
  const source_ref = safeReviewText(value.source_ref, 300);
  const source_type = safeReviewText(value.source_type, 40);
  return quote || source_ref ? { quote, source_ref, source_type } : null;
}

function parseCitations(value: unknown): ReviewCitation[] {
  return Array.isArray(value)
    ? value.slice(0, MAX_CITATIONS).map(parseCitation).filter((item): item is ReviewCitation => item !== null)
    : [];
}

function parseFieldAssessment(value: unknown) {
  if (!isRecord(value)) return undefined;
  const decision = safeReviewText(value.decision, 64);
  const risk_level = safeReviewText(value.risk_level, 32);
  const reasons = Array.isArray(value.reasons)
    ? value.reasons.slice(0, 12).map((item) => safeReviewText(item, 300)).filter((item): item is string => Boolean(item))
    : [];
  const citations = parseCitations(value.citations);
  return decision || risk_level || reasons.length || citations.length ? { decision, risk_level, reasons, citations } : undefined;
}

export function parseReviewAssessment(raw: string | null): ReviewAssessment | null {
  if (!raw) return null;
  try {
    const value: unknown = JSON.parse(raw);
    if (!isRecord(value)) return null;
    const milestoneCriteria = Array.isArray(value.milestone_criteria)
      ? value.milestone_criteria.slice(0, MAX_CRITERIA).flatMap((item) => {
        if (!isRecord(item)) return [];
        const result = ["met", "missing", "contradicted", "not_applicable"].includes(String(item.result))
          ? item.result as "met" | "missing" | "contradicted" | "not_applicable"
          : undefined;
        const criterion = {
          criterion_id: safeReviewText(item.criterion_id, 80), result,
          explanation: safeReviewText(item.explanation, 500), citations: parseCitations(item.citations),
        };
        return criterion.criterion_id || criterion.result || criterion.explanation || criterion.citations.length ? [criterion] : [];
      }) : [];
    const uncertainties = Array.isArray(value.uncertainties)
      ? value.uncertainties.slice(0, MAX_UNCERTAINTIES)
        .map((item) => safeReviewText(item, 400)).filter((item): item is string => Boolean(item))
      : [];
    const confidence: Record<string, number> = {};
    if (isRecord(value.confidence)) {
      for (const [rawName, item] of Object.entries(value.confidence).slice(0, MAX_CONFIDENCE_DIMENSIONS)) {
        const name = safeReviewText(rawName, 48);
        if (name && typeof item === "number" && Number.isFinite(item) && item >= 0 && item <= 1) confidence[name] = item;
      }
    }
    return {
      milestone_criteria: milestoneCriteria,
      milestone_assessment: parseFieldAssessment(value.milestone_assessment),
      status_assessment: parseFieldAssessment(value.status_assessment),
      ball_with_assessment: parseFieldAssessment(value.ball_with_assessment),
      next_action_assessment: parseFieldAssessment(value.next_action_assessment),
      uncertainties,
      contradictions: parseCitations(value.contradictions),
      confidence,
    };
  } catch { return null; }
}

export function parseReviewChanges(raw: string): ReviewChange[] {
  try {
    const value: unknown = JSON.parse(raw);
    if (!Array.isArray(value)) return [];
    return value.slice(0, 30).flatMap((item) => {
      if (!isRecord(item)) return [];
      const field = safeReviewText(item.field, 80);
      const new_value = field === "notes"
        ? safeReviewNotes(item.new_value)
        : safeReviewText(item.new_value, 500);
      if (!field || !new_value) return [];
      return [{
        field,
        new_value,
        old_value: field === "notes"
          ? safeReviewNotes(item.old_value)
          : safeReviewText(item.old_value, 500),
        reason: safeReviewText(item.reason, 500),
        evidence_ref: safeReviewText(item.evidence_ref, 300),
      }];
    });
  } catch { return []; }
}

export function collectReviewCitations(
  assessment: ReviewAssessment | null,
  changes: ReviewChange[],
  sourceRef: string | null,
): ReviewCitation[] {
  const slots = assessment ? [
    assessment.milestone_assessment, assessment.status_assessment,
    assessment.ball_with_assessment, assessment.next_action_assessment,
  ] : [];
  const fromAssessment = slots.flatMap((item) => item?.citations ?? []).concat(assessment?.contradictions ?? []);
  const legacy = changes.flatMap((item): ReviewCitation[] => item.evidence_ref ? [{ source_ref: item.evidence_ref }] : []);
  const safeSource = safeReviewText(sourceRef, 300);
  if (safeSource) legacy.push({ source_ref: safeSource });
  return [...fromAssessment, ...legacy].filter((item, index, all) =>
    Boolean(item.quote || item.source_ref)
    && all.findIndex((candidate) => candidate.quote === item.quote && candidate.source_ref === item.source_ref) === index,
  ).slice(0, MAX_CITATIONS);
}

export function isSafeReviewUrl(value: string): boolean {
  try {
    const url = new URL(value);
    return url.protocol === "https:" || url.protocol === "http:";
  } catch { return false; }
}
