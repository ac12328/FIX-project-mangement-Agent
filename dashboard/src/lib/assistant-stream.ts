import type { AssistantPart, AssistantStreamEvent } from "@/lib/assistant-types";

type SseFrame = { event: string; data: string };

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isNonemptyString(value: unknown): value is string {
  return typeof value === "string" && value.length > 0;
}

function isAssistantPart(value: unknown): value is AssistantPart {
  if (!isRecord(value) || !isNonemptyString(value.type)) return false;
  if (value.type !== "table") return true;
  return Array.isArray(value.columns)
    && value.columns.every((column: unknown) => isRecord(column)
      && isNonemptyString(column.key) && isNonemptyString(column.label))
    && Array.isArray(value.rows)
    && value.rows.every(isRecord);
}

/** Unknown event names may be future extensions; malformed known events are errors. */
export function parseAssistantStreamEvent(frame: SseFrame): AssistantStreamEvent | null {
  const knownEventFields: Record<string, readonly string[]> = {
    "turn.started": ["turn_id", "replayed"],
    progress: ["turn_id", "phase", "message"],
    "text.delta": ["turn_id", "delta"],
    "part.added": ["turn_id", "part"],
    "turn.completed": ["turn_id", "message_id", "replayed"],
    "turn.failed": ["turn_id", "error"],
  };
  if (!Object.hasOwn(knownEventFields, frame.event)) return null;

  let data: unknown;
  try {
    data = JSON.parse(frame.data);
  } catch {
    throw new Error("The server returned an invalid Workspace stream event.");
  }
  if (!isRecord(data) || !isNonemptyString(data.turn_id)
    || Object.keys(data).some((key) => !knownEventFields[frame.event].includes(key))) {
    throw new Error("The server returned an invalid Workspace stream event.");
  }

  switch (frame.event) {
    case "turn.started":
      if (data.replayed === undefined || typeof data.replayed === "boolean") {
        return { event: frame.event, data: { turn_id: data.turn_id, replayed: data.replayed } };
      }
      break;
    case "progress":
      if (typeof data.phase === "string" && typeof data.message === "string") {
        return { event: frame.event, data: { turn_id: data.turn_id, phase: data.phase, message: data.message } };
      }
      break;
    case "text.delta":
      if (typeof data.delta === "string") {
        return { event: frame.event, data: { turn_id: data.turn_id, delta: data.delta } };
      }
      break;
    case "part.added":
      if (isAssistantPart(data.part)) {
        return { event: frame.event, data: { turn_id: data.turn_id, part: data.part } };
      }
      break;
    case "turn.completed":
      if ((data.message_id === undefined || typeof data.message_id === "string" || data.message_id === null)
        && (data.replayed === undefined || typeof data.replayed === "boolean")) {
        return { event: frame.event, data: {
          turn_id: data.turn_id,
          ...(typeof data.message_id === "string" ? { message_id: data.message_id } : {}),
          replayed: data.replayed,
        } };
      }
      break;
    case "turn.failed":
      if (typeof data.error === "string") {
        return { event: frame.event, data: { turn_id: data.turn_id, error: data.error } };
      }
      break;
  }
  throw new Error("The server returned an invalid Workspace stream event.");
}
