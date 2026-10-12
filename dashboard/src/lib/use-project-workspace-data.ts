"use client";

import { useCallback } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import { getProject, getActivity, getEvidence, getProjectContacts, getProjectParties } from "@/lib/api";
import { getProjectSummary } from "@/lib/project-briefing-api";
import type { ProjectDetail } from "@/lib/types";
import { getUiSupport, uiV2Keys } from "@/lib/ui-v2-api";

export const projectWorkspaceKeys = {
  all: (id: string) => ["project-workspace", id] as const,
  section: (id: string, section: string) => ["project-workspace", id, section] as const,
};

/** Independent queries prevent secondary failures from hiding project truth. */
export function useProjectWorkspaceData(id: string, canWrite: boolean) {
  const client = useQueryClient();
  const enabled = Boolean(id);
  const project = useQuery({ queryKey: projectWorkspaceKeys.section(id, "project"), queryFn: () => getProject(id), enabled });
  const activity = useQuery({ queryKey: projectWorkspaceKeys.section(id, "activity"), queryFn: () => getActivity(id), enabled });
  const evidence = useQuery({ queryKey: projectWorkspaceKeys.section(id, "evidence"), queryFn: () => getEvidence(id), enabled });
  const contacts = useQuery({ queryKey: projectWorkspaceKeys.section(id, "contacts"), queryFn: () => getProjectContacts(id), enabled });
  const parties = useQuery({ queryKey: projectWorkspaceKeys.section(id, "parties"), queryFn: () => getProjectParties(id), enabled });
  const summary = useQuery({ queryKey: projectWorkspaceKeys.section(id, "summary"), queryFn: () => getProjectSummary(id), enabled });
  const desk = useQuery({
    queryKey: uiV2Keys.support({ related_project_id: id, status: "open", page_size: 8, queue: "all" }),
    queryFn: () => getUiSupport({ related_project_id: id, status: "open", page_size: 8, queue: "all" }),
    enabled: enabled && canWrite,
  });

  const { refetch: refetchDesk } = desk;
  const refresh = useCallback(
    async () => {
      await Promise.all([
        client.refetchQueries({ queryKey: projectWorkspaceKeys.all(id), type: "active" }),
        ...(canWrite ? [refetchDesk()] : []),
      ]);
    },
    [client, id, canWrite, refetchDesk],
  );
  const setProject = useCallback(
    (value: ProjectDetail) => { client.setQueryData(projectWorkspaceKeys.section(id, "project"), value); },
    [client, id],
  );
  const regenerateSummary = useCallback(
    () => client.fetchQuery({ queryKey: projectWorkspaceKeys.section(id, "summary"), queryFn: () => getProjectSummary(id, true), staleTime: 0 }),
    [client, id],
  );

  return { project, activity, evidence, contacts, parties, desk, summary, refresh, setProject, regenerateSummary };
}
