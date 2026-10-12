// dashboard/src/lib/milestones.ts

export interface MilestoneInfo {
  short: string;   // "M4"
  label: string;   // "Application Testing"
}

export const MILESTONE_SHORT: Record<string, MilestoneInfo> = {
  M1_NetworkSetup: { short: "M1", label: "Network Setup" },
  M2_UATSessionConfiguration: { short: "M2", label: "UAT Session Configuration" },
  M3_UATEnvironmentAccess: { short: "M3", label: "UAT Environment Access" },
  M4_ApplicationTesting: { short: "M4", label: "Application Testing" },
  M5_ConformanceTestCases: { short: "M5", label: "Conformance Test Cases" },
  M6_TestReportAndProductionPreparation: { short: "M6", label: "Test Report & Production Preparation" },
  M7_LogReviewAndProductionSessionDelivery: { short: "M7", label: "Log Review & Production Session Delivery" },
  M8_GoLive: { short: "M8", label: "Go Live" },
};
