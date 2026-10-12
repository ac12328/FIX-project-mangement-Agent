"use client";

import { useEffect, useEffectEvent } from "react";

import { API_BASE } from "./api-base";

const SSE_URL = `${API_BASE}/api/events`;

export type SSEEvent = {
  event_type: string;
  project_id: string | null;
  detail: Record<string, unknown>;
  surface: string;
  timestamp: number;
};

export function useEvents({
  enabled = true,
  eventTypes,
  onEvent,
  onRefresh,
  onReconnect,
}: {
  enabled?: boolean;
  eventTypes?: string[];
  onEvent?: (event: SSEEvent) => void;
  onRefresh?: () => void;
  onReconnect?: () => void;
}) {
  const eventTypesKey = eventTypes?.join(",") ?? "";
  const handleParsedEvent = useEffectEvent((parsed: SSEEvent) => {
    if (eventTypes && !eventTypes.includes(parsed.event_type)) return;
    onEvent?.(parsed);
    onRefresh?.();
  });

  const handleReconnect = useEffectEvent(() => onReconnect?.());

  useEffect(() => {
    if (!enabled) return;

    let currentSource: EventSource | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let disposed = false;
    let hasConnected = false;

    const types = eventTypes ?? [
      "review_approved",
      "review_rejected",
      "draft_approved",
      "draft_discarded",
      "project_updated",
    ];

    const handler = (event: MessageEvent) => {
      try {
        const parsed: SSEEvent = JSON.parse(event.data);
        handleParsedEvent(parsed);
      } catch {
        // Ignore malformed event payloads.
      }
    };

    const connect = () => {
      if (disposed) return;

      const es = new EventSource(SSE_URL, { withCredentials: true });
      currentSource = es;

      es.addEventListener("connected", () => {
        if (hasConnected) handleReconnect();
        hasConnected = true;
      });

      for (const type of types) es.addEventListener(type, handler);

      es.onerror = () => {
        es.close();
        currentSource = null;
        if (!disposed) reconnectTimer = setTimeout(connect, 3000);
      };
    };

    connect();

    return () => {
      disposed = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      currentSource?.close();
    };
  }, [enabled, eventTypes, eventTypesKey]);
}
