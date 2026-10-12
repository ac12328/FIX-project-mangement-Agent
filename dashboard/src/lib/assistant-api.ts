import { approveIntent, fetchJson, rejectIntent, streamSse } from "@/lib/api";
import type { components } from "@/lib/generated/api-schema";
import { parseAssistantStreamEvent } from "@/lib/assistant-stream";
import type {
  AssistantCapabilities,
  AssistantOperatorUpdatePart,
  AssistantMessagePage,
  AssistantStreamEvent,
  AssistantThread,
  AssistantThreadCreate,
  AssistantThreadPatch,
  AssistantTurnPayload,
} from "@/lib/assistant-types";

type OperatorUpdateReceipt = components["schemas"]["OperatorUpdateReceipt"];

export type OperatorUpdateCardResponse = Omit<
  components["schemas"]["OperatorUpdateCardResponse"],
  "part" | "missing_criteria"
> & {
  part?: AssistantOperatorUpdatePart | null;
  missing_criteria?: Array<{ id: string; description?: string; result?: string }>;
};

function queryString(values: Record<string, string | number | boolean | undefined>) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (value !== undefined && value !== "") params.set(key, String(value));
  }
  const query = params.toString();
  return query ? `?${query}` : "";
}

export const getAssistantCapabilities = () =>
  fetchJson<AssistantCapabilities>("/api/assistant/capabilities");

export function listAssistantThreads(options: {
  search?: string;
  archived?: boolean;
  projectId?: string;
  limit?: number;
  cursor?: string;
} = {}) {
  const query = queryString({
    search: options.search,
    archived: options.archived,
    project_id: options.projectId,
    limit: options.limit,
    cursor: options.cursor,
  });
  return fetchJson<components["schemas"]["AssistantThreadPage"]>(`/api/assistant/threads${query}`);
}

export const createAssistantThread = (payload: AssistantThreadCreate = {}) =>
  fetchJson<AssistantThread>("/api/assistant/threads", {
    method: "POST",
    body: JSON.stringify({
      title: payload.title ?? "New conversation",
      scope_mode: payload.scope_mode ?? "portfolio",
      project_ids: payload.project_ids ?? [],
      web_enabled: payload.web_enabled ?? false,
    }),
  });

export const getAssistantThread = (threadId: string) =>
  fetchJson<AssistantThread>(`/api/assistant/threads/${encodeURIComponent(threadId)}`);

export const patchAssistantThread = (threadId: string, payload: AssistantThreadPatch) =>
  fetchJson<AssistantThread>(`/api/assistant/threads/${encodeURIComponent(threadId)}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });

export const deleteAssistantThread = (threadId: string) =>
  fetchJson<{ deleted: true; thread_id: string }>(
    `/api/assistant/threads/${encodeURIComponent(threadId)}`,
    { method: "DELETE" },
  );

export function listAssistantMessages(
  threadId: string,
  options: { limit?: number; cursor?: string } = {},
) {
  const query = queryString({ limit: options.limit, cursor: options.cursor });
  return fetchJson<AssistantMessagePage>(
    `/api/assistant/threads/${encodeURIComponent(threadId)}/messages${query}`,
  );
}

export async function streamAssistantTurn(
  threadId: string,
  payload: AssistantTurnPayload,
  options: {
    signal?: AbortSignal;
    onEvent: (event: AssistantStreamEvent) => void;
    onReconnect?: (attempt: number) => void;
    reconnectAttempts?: number;
  },
) {
  const path = `/api/assistant/threads/${encodeURIComponent(threadId)}/turns`;
  const reconnectAttempts = Math.max(0, options.reconnectAttempts ?? 1);
  for (let attempt = 0; ; attempt += 1) {
    let sawTerminalEvent = false;
    const emit = (message: { event: string; data: string }) => {
      let data: unknown;
      try {
        data = JSON.parse(message.data);
      } catch {
        data = { error: "The server returned an invalid stream event." };
      }
      const terminal = typeof data === "object" && data !== null && !Array.isArray(data)
        ? data as Record<string, unknown>
        : null;
      const part = terminal?.part;
      if (message.event === "part.added" && part && typeof part === "object"
        && !Array.isArray(part) && (part as Record<string, unknown>).type === "table") {
        // Only tighten the table contract; preserve existing forwarding for all other frames.
        parseAssistantStreamEvent(message);
      }
      if (message.event === "turn.completed" || message.event === "turn.failed") {
        try {
          // Count only terminals accepted by the shared producer/consumer contract.
          sawTerminalEvent ||= parseAssistantStreamEvent(message) !== null;
        } catch {
          // Preserve the existing forward-compatible frame delivery, but never
          // turn a malformed terminal into a successful clean EOF.
        }
      }
      options.onEvent({ event: message.event, data } as AssistantStreamEvent);
    };
    try {
      await streamSse(path, {
        method: "POST",
        body: JSON.stringify(payload),
        signal: options.signal,
        onEvent: emit,
      });
      if (options.signal?.aborted) throw new DOMException("The request was cancelled.", "AbortError");
      if (!sawTerminalEvent) throw new Error("The AI response stream ended without a terminal event.");
      return;
    } catch (error) {
      if (options.signal?.aborted || attempt >= reconnectAttempts) throw error;
      options.onReconnect?.(attempt + 1);
    }
  }
}

export const approveAssistantAction = (actionId: string) => approveIntent(actionId, true);
export const rejectAssistantAction = (actionId: string) => rejectIntent(actionId);

export const stageOperatorUpdate = (
  projectId: string,
  payload: {
    thread_id: string;
    fields: Record<string, string>;
    reason: string;
    evidence_ref?: string | null;
  },
) =>
  fetchJson<OperatorUpdateCardResponse>(
    `/api/projects/${encodeURIComponent(projectId)}/operator-updates`,
    { method: "POST", body: JSON.stringify(payload) },
  );

export const confirmOperatorUpdate = (
  projectId: string,
  operationId: string,
  payload: { thread_id: string; reason?: string; evidence_ref?: string | null },
) =>
  fetchJson<OperatorUpdateReceipt>(
    `/api/projects/${encodeURIComponent(projectId)}/operator-updates/${encodeURIComponent(operationId)}/confirm`,
    { method: "POST", body: JSON.stringify(payload) },
  );

export const cancelOperatorUpdate = (
  projectId: string,
  operationId: string,
  payload: { thread_id: string },
) =>
  fetchJson<OperatorUpdateReceipt>(
    `/api/projects/${encodeURIComponent(projectId)}/operator-updates/${encodeURIComponent(operationId)}/cancel`,
    { method: "POST", body: JSON.stringify(payload) },
  );

export const getOperatorUpdate = (projectId: string, operationId: string) =>
  fetchJson<OperatorUpdateCardResponse>(
    `/api/projects/${encodeURIComponent(projectId)}/operator-updates/${encodeURIComponent(operationId)}`,
  );
