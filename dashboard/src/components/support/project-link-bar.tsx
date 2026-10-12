"use client";

import Link from "next/link";
import { Badge } from "@/components/ui/badge";
import type { ProjectLinkInfo } from "@/lib/support-api";

interface ProjectLinkBarProps {
  project: ProjectLinkInfo;
}

export default function ProjectLinkBar({ project }: ProjectLinkBarProps) {
  if (!project.has_project) return null;

  return (
    <div className="flex items-center gap-3 rounded-xl border border-primary/20 bg-primary/5 px-5 py-3 text-[13px]">
      <span className="text-muted-foreground">Related Project:</span>
      <Link href={`/projects/${project.project_id}`} className="font-semibold text-primary hover:underline">
        {project.project_name ?? project.project_id}{project.counterparty ? ` \u2014 ${project.counterparty}` : ""}
      </Link>
      {project.current_milestone && (
        <Badge variant="outline" className="bg-primary/10 text-primary border-primary/30 text-xs">{project.current_milestone}</Badge>
      )}
    </div>
  );
}
