import type {
  AssistantIntent,
  AssistantMessage,
  AssistantPart,
  AssistantStreamEvent,
  AssistantTablePart,
  AssistantThread,
  AssistantTurnPayload,
} from "@/lib/assistant-types";

export const WORKSPACE_COMMANDS = [
  { name: "new", usage: "/new", description: "Start a new conversation." },
  { name: "resume", usage: "/resume", description: "Find and reopen a saved conversation." },
  { name: "project", usage: "/project", description: "Add one or more project contexts." },
  { name: "portfolio", usage: "/portfolio", description: "Switch the conversation to full portfolio context." },
  { name: "status", usage: "/status", description: "Analyze the active context." },
  { name: "compare", usage: "/compare", description: "Compare at least two selected projects." },
  { name: "draft", usage: "/draft [request]", description: "Create an email draft without sending it." },
  { name: "web", usage: "/web on|off", description: "Control external web search." },
  { name: "help", usage: "/help", description: "Show commands, capabilities, and permissions." },
] as const;

export type WorkspaceCommandName = (typeof WORKSPACE_COMMANDS)[number]["name"];

export interface ParsedWorkspaceCommand {
  name: string;
  arguments: string;
}

export function parseWorkspaceCommand(value: string): ParsedWorkspaceCommand | null {
  const input = value.trim();
  if (!input.startsWith("/")) return null;
  const [token, ...rest] = input.slice(1).split(/\s+/);
  if (!token) return null;
  return { name: token.toLowerCase(), arguments: rest.join(" ").trim() };
}

export function commandSuggestions(value: string) {
  const input = value.trimStart();
  if (!input.startsWith("/") || input.slice(1).includes(" ")) return [];
  const needle = input.slice(1).toLowerCase();
  return WORKSPACE_COMMANDS.filter((command) => command.name.startsWith(needle));
}

export function createClientTurnId(): string {
  const value = globalThis.crypto?.randomUUID?.()
    ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `workspace-${value}`;
}

export function buildTurnPayload(input: string, thread: AssistantThread): AssistantTurnPayload {
  const parsed = parseWorkspaceCommand(input);
  let intent: AssistantIntent | undefined;
  if (parsed?.name === "status") {
    intent = { type: "status", project_ids: thread.project_ids };
  } else if (parsed?.name === "compare") {
    intent = { type: "compare", project_ids: thread.project_ids };
  } else if (parsed?.name === "draft") {
    intent = {
      type: "draft",
      project_ids: thread.project_ids,
      prompt: parsed.arguments || "Draft a concise project update email.",
    };
  }
  return {
    client_turn_id: createClientTurnId(),
    message: input.trim(),
    command: parsed?.name,
    intent,
  };
}

export interface StreamingTurnState {
  turnId: string | null;
  text: string;
  parts: AssistantPart[];
  progress: string | null;
  status: "idle" | "running" | "completed" | "failed";
  error: string | null;
}

export const EMPTY_STREAMING_TURN: StreamingTurnState = {
  turnId: null,
  text: "",
  parts: [],
  progress: null,
  status: "idle",
  error: null,
};

export function reduceStreamingTurn(
  state: StreamingTurnState,
  event: AssistantStreamEvent,
): StreamingTurnState {
  if (event.event === "turn.started") {
    return { ...state, turnId: event.data.turn_id, status: "running", error: null };
  }
  if (event.event === "progress") {
    return { ...state, turnId: event.data.turn_id, progress: event.data.message, status: "running" };
  }
  if (event.event === "text.delta") {
    return { ...state, turnId: event.data.turn_id, text: state.text + event.data.delta, status: "running" };
  }
  if (event.event === "part.added") {
    return { ...state, turnId: event.data.turn_id, parts: [...state.parts, event.data.part], status: "running" };
  }
  if (event.event === "turn.completed") {
    return { ...state, turnId: event.data.turn_id, progress: null, status: "completed" };
  }
  return {
    ...state,
    turnId: event.data.turn_id,
    progress: null,
    status: "failed",
    error: event.data.error,
  };
}

function csvCell(value: unknown): string {
  const text = value === null || value === undefined
    ? ""
    : typeof value === "object" ? JSON.stringify(value) : String(value);
  return /[",\r\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
}

export function tablePartToCsv(part: AssistantTablePart): string {
  const header = part.columns.map((column) => csvCell(column.label)).join(",");
  const rows = part.rows.map((row) =>
    part.columns.map((column) => csvCell(row[column.key])).join(","),
  );
  return [header, ...rows].join("\r\n");
}

export function findRegenerationPrompt(
  messages: AssistantMessage[],
  response: AssistantMessage,
): string | null {
  const responseIndex = messages.findIndex((message) => message.message_id === response.message_id);
  const end = responseIndex >= 0 ? responseIndex : messages.length;

  if (response.turn_id) {
    for (let index = end - 1; index >= 0; index -= 1) {
      const candidate = messages[index];
      if (candidate.role === "user" && candidate.turn_id === response.turn_id) {
        return candidate.content.trim() || null;
      }
    }
  }
  for (let index = end - 1; index >= 0; index -= 1) {
    const candidate = messages[index];
    if (candidate.role === "user" && candidate.content.trim()) {
      return candidate.content.trim();
    }
  }
  return null;
}


export function isKnownWorkspaceCommand(name: string): name is WorkspaceCommandName {
  return WORKSPACE_COMMANDS.some((command) => command.name === name);
}
