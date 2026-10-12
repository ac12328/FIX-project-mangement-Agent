export const WORK_QUEUE_ID = "action-queue";
export const WORK_QUEUE_HREF = `/?queue=open#${WORK_QUEUE_ID}`;
export const WORK_QUEUE_FOCUS_EVENT = "agentfixb:focus-work-queue";

export function isWorkQueueRequested(location: Pick<Location, "search" | "hash">): boolean {
  return new URLSearchParams(location.search).get("queue") === "open"
    || location.hash === `#${WORK_QUEUE_ID}`;
}

export function updateWorkQueueLocation(): void {
  if (typeof window === "undefined") return;
  const current = `${window.location.pathname}${window.location.search}${window.location.hash}`;
  if (current !== WORK_QUEUE_HREF) {
    window.history.pushState(null, "", WORK_QUEUE_HREF);
  }
}

export function requestWorkQueueFocus(): void {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new Event(WORK_QUEUE_FOCUS_EVENT));
}
