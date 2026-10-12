import type {
  AssistantActionPart,
  AssistantFollowUpQuestionsPart,
  AssistantMessage,
  AssistantOperatorUpdatePart,
  AssistantPart,
  AssistantSourcePart,
} from "@/lib/assistant-types";

function allWorkspaceParts(messages: AssistantMessage[], liveParts: AssistantPart[]): AssistantPart[] {
  return [...messages.flatMap((message) => message.parts), ...liveParts];
}

export function selectAssistantSources(
  messages: AssistantMessage[],
  liveParts: AssistantPart[] = [],
): AssistantSourcePart[] {
  const seen = new Set<string>();
  return allWorkspaceParts(messages, liveParts)
    .filter((part): part is AssistantSourcePart => part.type === "source")
    .filter((source) => {
      if (!source.source_id || seen.has(source.source_id)) return false;
      seen.add(source.source_id);
      return true;
    });
}

export function selectPendingAssistantActions(
  messages: AssistantMessage[],
  liveParts: AssistantPart[] = [],
): Array<AssistantActionPart | AssistantOperatorUpdatePart> {
  const seen = new Set<string>();
  return allWorkspaceParts(messages, liveParts)
    .filter((part): part is AssistantActionPart | AssistantOperatorUpdatePart => (
      part.type === "action" || part.type === "operator_update"
    ))
    .filter((item) => {
      if (item.type === "operator_update") {
        const key = item.operation_id || `intake:${item.project_id}:${item.fields?.current_milestone || ""}`;
        if (seen.has(key)) return false;
        seen.add(key);
        return ["needs_intake", "pending"].includes(String(item.status).toLowerCase());
      }
      if (!item.action_id || seen.has(item.action_id)) return false;
      seen.add(item.action_id);
      return ["pending", "proposed"].includes(item.status.toLowerCase());
    });
}

function followUpQuestions(part: AssistantPart | undefined): string[] {
  if (!part || part.type !== "follow_up_questions") return [];
  const questions = (part as AssistantFollowUpQuestionsPart).questions;
  if (!Array.isArray(questions)) return [];
  const seen = new Set<string>();
  return questions
    .filter((question): question is string => typeof question === "string")
    .map((question) => question.trim())
    .filter((question) => {
      const key = question.toLocaleLowerCase();
      if (!question || seen.has(key)) return false;
      seen.add(key);
      return true;
    })
    .slice(0, 3);
}

export function selectLatestAssistantFollowUpQuestions(
  messages: AssistantMessage[],
  liveParts: AssistantPart[] = [],
): string[] {
  const livePart = [...liveParts].reverse().find((part) => part.type === "follow_up_questions");
  if (livePart) return followUpQuestions(livePart);

  const latestAssistant = [...messages].reverse().find((message) => message.role === "assistant");
  if (!latestAssistant) return [];
  const savedPart = [...latestAssistant.parts].reverse().find((part) => part.type === "follow_up_questions");
  return followUpQuestions(savedPart);
}
