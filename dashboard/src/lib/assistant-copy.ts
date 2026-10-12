import type { AssistantMessage, AssistantSourcePart } from "@/lib/assistant-types";
import {
  assistantSourceHref,
  sanitizeAssistantText,
} from "@/lib/assistant-sources";

const EXTERNAL_URL_RE = /^https?:\/\//i;

function sourceParts(message: AssistantMessage): AssistantSourcePart[] {
  return message.parts.filter((part): part is AssistantSourcePart => part.type === "source");
}

function absoluteSourceHref(href: string, origin?: string): string {
  if (EXTERNAL_URL_RE.test(href)) return href;
  if (!origin) return href;

  try {
    return new URL(href, origin).toString();
  } catch {
    return href;
  }
}

export function formatAssistantMessageForCopy(
  message: AssistantMessage,
  options: { origin?: string } = {},
): string {
  const sources = sourceParts(message);
  const byId = new Map(sources.map((source, index) => [source.source_id, { source, number: index + 1 }]));
  const sanitized = sanitizeAssistantText(message.content);

  return sanitized.replace(/\[\[source:([^\]]+)\]\]/g, (_marker, sourceId: string) => {
    const entry = byId.get(sourceId);
    if (!entry) return "";

    const href = assistantSourceHref(entry.source);
    if (!href) return `[${entry.number}]`;

    return `[${entry.number}](${absoluteSourceHref(href, options.origin)})`;
  });
}
