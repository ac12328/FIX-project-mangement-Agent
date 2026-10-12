import type { AssistantSourcePart } from "@/lib/assistant-types";

const EXTERNAL_URL_RE = /^https?:\/\//i;
const INTERNAL_TOOL_ARTIFACT_RE = /\bmonads0x[a-z0-9_]+\b/gi;

export function assistantSourceHref(source: AssistantSourcePart): string | null {
  const url = source.url?.trim();
  if (url && (EXTERNAL_URL_RE.test(url) || url.startsWith("/"))) return url;
  if (source.project_id) return `/projects/${encodeURIComponent(source.project_id)}`;
  return null;
}

export function isExternalAssistantSource(source: AssistantSourcePart): boolean {
  return Boolean(source.url && EXTERNAL_URL_RE.test(source.url));
}

export function assistantSourceLabel(source: AssistantSourcePart, index: number): string {
  return source.title?.trim() || `Source ${index + 1}`;
}

export function assistantCitationAnchor(sourceId: string): string {
  return `#assistant-source-${encodeURIComponent(sourceId)}`;
}

export function sourceIdFromCitationAnchor(href?: string): string | null {
  const prefix = "#assistant-source-";
  if (!href?.startsWith(prefix)) return null;
  try {
    return decodeURIComponent(href.slice(prefix.length));
  } catch {
    return null;
  }
}

export function sanitizeAssistantText(text: string): string {
  return text
    .replace(INTERNAL_TOOL_ARTIFACT_RE, "")
    .replace(/[ \t]+$/gm, "")
    .trim();
}

export function replaceAssistantCitations(
  text: string,
  sources: AssistantSourcePart[],
): string {
  const sourceNumbers = new Map(sources.map((source, index) => [source.source_id, index + 1]));
  return text.replace(/\[\[source:([^\]]+)\]\]/g, (marker, sourceId: string) => {
    const number = sourceNumbers.get(sourceId);
    if (!number) return "";
    return `[${number}](${assistantCitationAnchor(sourceId)})`;
  });
}
