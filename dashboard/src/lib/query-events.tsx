"use client";

import { useQueryClient } from "@tanstack/react-query";

import { uiV2Keys } from "@/lib/ui-v2-api";
import { useEvents, type SSEEvent } from "@/lib/useEvents";

const EVENT_TYPES = [
  "review_approved",
  "review_rejected",
  "review_superseded",
  "review_queued",
  "draft_approval_requested",
  "draft_approved",
  "draft_discarded",
  "draft_created",
  "draft_sent",
  "draft_edited",
  "project_created",
  "project_updated",
  "project_email_processed",
  "followup_completed",
  "followup_cancelled",
  "ticket_updated",
  "support_escalated",
  "support_reclassified",
];

export function invalidationKeysForEvent(event: Pick<SSEEvent, "event_type">) {
  const type = event.event_type;
  const keys: ReadonlyArray<readonly unknown[]> = [uiV2Keys.overview(), ["ui-v2", "activity"]];

  if (type.startsWith("review_")) return [...keys, ["ui-v2", "reviews"], ["ui-v2", "work-items"]];
  if (type.startsWith("draft_")) return [...keys, ["ui-v2", "drafts"], ["ui-v2", "work-items"]];
  if (type.startsWith("support_") || type === "ticket_updated") {
    return [...keys, ["ui-v2", "support"], ["ui-v2", "search"]];
  }
  if (type.startsWith("followup_")) return [...keys, ["ui-v2", "work-items"], ["ui-v2", "projects"]];
  if (type.startsWith("project_")) {
    return [...keys, ["ui-v2", "projects"], ["ui-v2", "parties"], ["ui-v2", "search"]];
  }
  return keys;
}

export function QueryEventInvalidator({ enabled }: { enabled: boolean }) {
  const queryClient = useQueryClient();

  useEvents({
    enabled,
    eventTypes: EVENT_TYPES,
    // Events missed while disconnected cannot be replayed by the current SSE API.
    onReconnect: () => { void queryClient.invalidateQueries({ queryKey: ["ui-v2"] }); },
    onEvent: (event) => {
      for (const queryKey of invalidationKeysForEvent(event)) {
        void queryClient.invalidateQueries({ queryKey });
      }
    },
  });

  return null;
}
