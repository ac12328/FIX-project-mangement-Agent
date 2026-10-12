import type { components } from "@/lib/generated/api-schema";

export type UiProblem = components["schemas"]["ProblemResponse"];
export type UiSectionStatus = components["schemas"]["SectionStatus"];
export type UiMetric = components["schemas"]["MetricReadModel"];
export type UiActionItem = components["schemas"]["ActionItemReadModel"];
export type UiRiskItem = components["schemas"]["RiskItemReadModel"];
export type UiRiskSummary = components["schemas"]["RiskSummaryReadModel"];
export type UiUpcomingItem = components["schemas"]["UpcomingItemReadModel"];
export type UiOverview = components["schemas"]["UiOverviewResponse"];
export type UiSearchItem = components["schemas"]["SearchItemReadModel"];
export type UiSearchResponse = components["schemas"]["UiSearchResponse"];
export type UiFacet = components["schemas"]["FacetReadModel"];
export type UiFacetValue = components["schemas"]["FacetValue"];
export type UiWorkItemsResponse = components["schemas"]["WorkItemsResponse"];
export type UiProject = components["schemas"]["ProjectListItemReadModel"];
export type UiProjectsResponse = components["schemas"]["ProjectsResponse"];
export type UiCompletedProject = {
  project_id: string;
  client_name?: string | null;
  market?: string | null;
  project_status: string;
  ball_with: string;
  current_milestone?: string | null;
  operator_id: string;
  completed_at: string;
  days_since_completed: number;
};
export type UiCompletedProjectsResponse = {
  items: UiCompletedProject[];
  facets: UiFacet[];
  page: number;
  page_size: number;
  total: number;
  generated_at: string;
  days: number;
};
export type UiCompletedProjectFilters = {
  days?: 14 | 30;
  q?: string;
  page?: number;
  page_size?: number;
};
export type UiParty = components["schemas"]["PartyListItemReadModel"];
export type UiPartiesResponse = components["schemas"]["PartiesResponse"];
export type UiSupportItem = components["schemas"]["SupportListItemReadModel"];
export type UiSupportResponse = components["schemas"]["SupportResponse"];
export type UiDraftItem = components["schemas"]["DraftListItemReadModel"];
export type UiDraftsResponse = components["schemas"]["DraftsResponse"];
export type UiReviewItem = components["schemas"]["ReviewListItemReadModel"];
export type UiReviewsResponse = components["schemas"]["ReviewsResponse"];
export type UiActivityItem = components["schemas"]["ActivityListItemReadModel"];
export type UiActivityResponse = components["schemas"]["ActivityResponse"];
export type UiTimelineItem = components["schemas"]["TimelineListItemReadModel"];
export type UiTimelineResponse = components["schemas"]["TimelineResponse"];

export type UiProjectFilters = {
  q?: string;
  status?: string;
  ball_with?: string;
  milestone?: string;
  market?: string;
  health?: "red" | "yellow" | "green";
  active_only?: boolean;
  sort?: "risk" | "name" | "updated" | "go_live";
  mine?: boolean;
  owner?: string;
  page?: number;
  page_size?: number;
};

export type UiPartyFilters = {
  org_type?: "client" | "vendor" | "regulator" | "internal" | "unknown";
  q?: string;
  sort?: "name" | "projects" | "recent";
  page?: number;
  page_size?: number;
};

export type UiWorkFilters = {
  kind?: "review" | "draft" | "approval" | "followup";
  priority?: "critical" | "high" | "medium" | "low";
  status?: string;
  sort?: "priority" | "due" | "oldest";
  page?: number;
  page_size?: number;
};

export type UiSupportFilters = {
  q?: string;
  status?: string;
  severity?: string;
  queue?: string;
  case_type?: string;
  unidentified_only?: boolean;
  related_project_id?: string;
  sort?: "priority" | "updated" | "title";
  page?: number;
  page_size?: number;
};

export type DeskSupportItem = {
  ticket_id: string;
  title: string;
  display_title: string;
  category: string;
  severity: string;
  status: string;
  related_project_id?: string | null;
  related_project_name?: string | null;
  counterparty?: string | null;
  party_name?: string | null;
  reporter_email?: string | null;
  account_hint?: string | null;
  assigned_team?: string | null;
  updated_at?: string | null;
  last_activity_at?: string | null;
  last_sender?: string | null;
  last_subject?: string | null;
  last_preview?: string | null;
  age_days?: number | null;
  ball_with?: string | null;
  case_type: string;
  queue: string;
  next_action: string;
  on_project: boolean;
  is_noise: boolean;
  already_answered: boolean;
  mixed_thread: boolean;
  can_execute: boolean;
};

export type DeskSupportResponse = {
  items: DeskSupportItem[];
  total: number;
  page: number;
  page_size: number;
  generated_at: string;
  queue_counts: Record<string, number>;
};

export type UiDraftFilters = {
  q?: string;
  status?: string;
  page?: number;
  page_size?: number;
};

export type UiReviewFilters = {
  q?: string;
  risk?: string;
  page?: number;
  page_size?: number;
};

export type UiActivityFilters = {
  q?: string;
  action_type?: string;
  project_id?: string;
  page?: number;
  page_size?: number;
};

export type UiTimelineFilters = {
  q?: string;
  health?: "red" | "yellow" | "green";
  schedule_status?: "overdue" | "due_soon" | "scheduled" | "no_date" | "invalid";
  sort?: "milestone" | "go_live" | "risk" | "name";
  page?: number;
  page_size?: number;
};
