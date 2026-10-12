import { fetchJson } from "@/lib/api";
import type {
  UiActivityFilters,
  UiCompletedProjectFilters,
  UiCompletedProjectsResponse,
  UiActivityResponse,
  UiDraftFilters,
  UiDraftsResponse,
  UiOverview,
  UiPartiesResponse,
  UiPartyFilters,
  UiProjectFilters,
  UiProjectsResponse,
  UiReviewFilters,
  UiReviewsResponse,
  UiSearchResponse,
  DeskSupportResponse,
  UiSupportFilters,
  UiTimelineFilters,
  UiTimelineResponse,
  UiWorkFilters,
  UiWorkItemsResponse,
} from "@/lib/ui-v2-types";

function queryString(params: Record<string, string | number | boolean | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === "") continue;
    search.set(key, String(value));
  }
  const value = search.toString();
  return value ? `?${value}` : "";
}

export const uiV2Keys = {
  all: ["ui-v2"] as const,
  overview: () => ["ui-v2", "overview"] as const,
  search: (query: string, types?: string) => ["ui-v2", "search", query, types ?? "all"] as const,
  workItems: (filters: UiWorkFilters) => ["ui-v2", "work-items", filters] as const,
  projects: (filters: UiProjectFilters) => ["ui-v2", "projects", filters] as const,
  completedProjects: (filters: UiCompletedProjectFilters) => ["ui-v2", "completed-projects", filters] as const,
  parties: (filters: UiPartyFilters) => ["ui-v2", "parties", filters] as const,
  support: (filters: UiSupportFilters) => ["ui-v2", "support", filters] as const,
  drafts: (filters: UiDraftFilters) => ["ui-v2", "drafts", filters] as const,
  reviews: (filters: UiReviewFilters) => ["ui-v2", "reviews", filters] as const,
  activity: (filters: UiActivityFilters) => ["ui-v2", "activity", filters] as const,
  timeline: (filters: UiTimelineFilters) => ["ui-v2", "timeline", filters] as const,
};

export const getUiOverview = () => fetchJson<UiOverview>("/api/ui/v2/overview");

export const searchUi = (query: string, types?: string, limit = 12) =>
  fetchJson<UiSearchResponse>(
    `/api/ui/v2/search${queryString({ q: query, types, limit })}`,
  );

export const getUiWorkItems = (filters: UiWorkFilters = {}) =>
  fetchJson<UiWorkItemsResponse>(`/api/ui/v2/work-items${queryString(filters)}`);

export const getUiProjects = (filters: UiProjectFilters = {}) =>
  fetchJson<UiProjectsResponse>(`/api/ui/v2/projects${queryString(filters)}`);

export const getUiCompletedProjects = (filters: UiCompletedProjectFilters = {}) =>
  fetchJson<UiCompletedProjectsResponse>(`/api/ui/v2/completed-projects${queryString(filters)}`);

export const getUiParties = (filters: UiPartyFilters = {}) =>
  fetchJson<UiPartiesResponse>(`/api/ui/v2/parties${queryString(filters)}`);

export const getUiSupport = (filters: UiSupportFilters = {}) =>
  fetchJson<DeskSupportResponse>(`/api/ui/v2/support${queryString(filters)}`);

export const getUiDrafts = (filters: UiDraftFilters = {}) =>
  fetchJson<UiDraftsResponse>(`/api/ui/v2/drafts${queryString(filters)}`);

export const getUiReviews = (filters: UiReviewFilters = {}) =>
  fetchJson<UiReviewsResponse>(`/api/ui/v2/reviews${queryString(filters)}`);

export const getUiActivity = (filters: UiActivityFilters = {}) =>
  fetchJson<UiActivityResponse>(`/api/ui/v2/activity${queryString(filters)}`);

export const getUiTimeline = (filters: UiTimelineFilters = {}) =>
  fetchJson<UiTimelineResponse>(`/api/ui/v2/timeline${queryString(filters)}`);
