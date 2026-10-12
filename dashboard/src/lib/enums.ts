/** Domain vocabulary mirrored from src/models/enums.py — wire format is strings. */

export const PROJECT_STATUSES = [
  "Active",
  "Blocked",
  "Waiting",
  "Completed",
  "Archived",
  "Cancelled",
] as const;
export type ProjectStatus = (typeof PROJECT_STATUSES)[number];

export const BALL_WITH = ["Internal", "External", "Shared", "Unknown"] as const;
export type BallWith = (typeof BALL_WITH)[number];

export const TICKET_STATUSES = [
  "open",
  "investigating",
  "waiting_external",
  "waiting_internal",
  "resolved",
  "closed",
] as const;
export type TicketStatus = (typeof TICKET_STATUSES)[number];

export const DRAFT_STATUSES = [
  "created",
  "in_review",
  "approved",
  "sent",
  "discarded",
  "send_failed",
] as const;
export type DraftStatus = (typeof DRAFT_STATUSES)[number];

export const MILESTONES = [
  "M1_NetworkSetup",
  "M2_UATSessionConfiguration",
  "M3_UATEnvironmentAccess",
  "M4_ApplicationTesting",
  "M5_ConformanceTestCases",
  "M6_TestReportAndProductionPreparation",
  "M7_LogReviewAndProductionSessionDelivery",
  "M8_GoLive",
] as const;
export type Milestone = (typeof MILESTONES)[number];

export const PROJECT_TIERS = ["high", "medium", "low"] as const;
export type ProjectTier = (typeof PROJECT_TIERS)[number];
