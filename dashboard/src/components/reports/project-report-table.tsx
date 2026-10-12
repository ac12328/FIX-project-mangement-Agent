"use client";

import Link from "next/link";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { completedDate, milestoneLabel, type ReportProject } from "@/lib/project-report";

export function ProjectReportTable({ items, kind }: { items: ReportProject[]; kind: "postponed" | "completed" }) {
  if (!items.length) return <p className="rounded-xl border bg-card p-5 text-sm text-muted-foreground">None in this snapshot.</p>;
  const completed = kind === "completed";
  const projectName = (project: ReportProject) => <div className="min-w-0">
    <Link href={`/projects/${encodeURIComponent(project.project_id)}`} className="inline-flex min-h-11 items-center break-words rounded text-sm font-semibold text-primary underline-offset-4 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">{project.client_name || project.project_id}</Link>
    <p className="text-xs leading-5 text-muted-foreground">{project.project_id} / {project.market || "Market not recorded"}</p>
  </div>;
  return <>
    <div className="hidden overflow-hidden rounded-xl border bg-card lg:block">
      <Table className="w-full table-fixed" aria-label={completed ? "Completed projects" : "Postponed projects"}>
        <TableHeader className="bg-muted/40"><TableRow>
          <TableHead className="w-[24%] px-4 text-xs text-muted-foreground">Project</TableHead>
          <TableHead className={`${completed ? "w-[48%]" : "w-[24%]"} px-3 text-xs text-muted-foreground`}>Current stage</TableHead>
          <TableHead className="w-[14%] px-3 text-xs text-muted-foreground">{completed ? "Completed (HKT)" : "Previous target"}</TableHead>
          {!completed && <TableHead className="w-[38%] px-4 text-xs text-muted-foreground">Recorded next action / ball with</TableHead>}
        </TableRow></TableHeader>
        <TableBody>{items.map(project => <TableRow key={project.project_id}>
          <TableCell className="whitespace-normal px-4 py-3 align-top">{projectName(project)}</TableCell>
          <TableCell className="whitespace-normal px-3 py-4 align-top text-sm leading-6">{milestoneLabel(project.current_milestone)}</TableCell>
          <TableCell className="whitespace-normal px-3 py-4 align-top text-sm">{completed ? completedDate(project.completed_at) : project.target_go_live || "Not recorded"}{(project.data_notes ?? []).map(note => <p key={note} className="mt-2 text-xs leading-5 text-muted-foreground">{note}</p>)}</TableCell>
          {!completed && <TableCell className="whitespace-normal px-4 py-4 align-top"><p className="break-words text-sm leading-6">{project.next_action || "Not recorded"}</p><p className="mt-2 text-xs text-muted-foreground">Ball with / {project.ball_with}</p></TableCell>}
        </TableRow>)}</TableBody>
      </Table>
    </div>
    <div className="grid gap-3 md:grid-cols-2 lg:hidden">
      {items.map(project => <article key={project.project_id} className="min-w-0 rounded-xl border bg-card p-4">
        {projectName(project)}
        <p className="mt-2 text-xs leading-5 text-muted-foreground">{milestoneLabel(project.current_milestone)}</p>
        <dl className="mt-3 grid grid-cols-2 gap-2 text-sm">
          <div><dt className="text-xs text-muted-foreground">{completed ? "Completed (HKT)" : "Previous target"}</dt><dd className="mt-1">{completed ? completedDate(project.completed_at) : project.target_go_live || "Not recorded"}</dd></div>
          {!completed && <div><dt className="text-xs text-muted-foreground">Ball with</dt><dd className="mt-1">{project.ball_with}</dd></div>}
        </dl>
        {(project.data_notes ?? []).map(note => <p key={note} className="mt-2 text-xs leading-5 text-muted-foreground">{note}</p>)}
        {!completed && <div className="mt-3 border-t pt-3"><p className="text-xs text-muted-foreground">Recorded next action</p><p className="mt-1 break-words text-sm leading-6">{project.next_action || "Not recorded"}</p></div>}
      </article>)}
    </div>
  </>;
}
