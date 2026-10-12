import type { UiActionItem } from "@/lib/ui-v2-types";

const PRIORITY_ORDER: Record<UiActionItem["priority"], number> = {
  critical: 0,
  high: 1,
  medium: 2,
  low: 3,
};

export function normalizeWorkItems(items: UiActionItem[] | undefined): UiActionItem[] {
  return [...(items ?? [])].sort((left, right) => {
    const priority = PRIORITY_ORDER[left.priority] - PRIORITY_ORDER[right.priority];
    if (priority !== 0) return priority;
    const leftDue = left.due_at ?? "9999-12-31";
    const rightDue = right.due_at ?? "9999-12-31";
    if (leftDue !== rightDue) return leftDue.localeCompare(rightDue);
    return left.title.localeCompare(right.title);
  });
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return "Not scheduled";
  const date = new Date(value.length === 10 ? `${value}T00:00:00Z` : value);
  if (Number.isNaN(date.getTime())) return "Invalid date";
  return new Intl.DateTimeFormat("en-GB", {
    day: "numeric",
    month: "short",
    year: "numeric",
    timeZone: "UTC",
  }).format(date);
}

export function formatTimestamp(value: string | null | undefined): string {
  if (!value) return "Unknown";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Invalid date";
  return new Intl.DateTimeFormat("en-GB", {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

export function formatAge(days: number | null | undefined): string {
  if (days === null || days === undefined) return "Age unknown";
  if (days <= 0) return "Today";
  return `${days} day${days === 1 ? "" : "s"}`;
}
