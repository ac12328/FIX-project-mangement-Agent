"use client";

import { useState, type Ref } from "react";
import Link from "next/link";
import { ChevronRight, Info } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { ProjectOverviewImage } from "@/components/reports/project-overview-image";
import { REPORT_STAGES, milestoneIndex, milestoneLabel, orderedActiveProjects, targetLabel, type ProjectReport, type ReportProject } from "@/lib/project-report";

function StageCells({ project, labelled = false }: { project: ReportProject; labelled?: boolean }) {
  const stageIndex = milestoneIndex(project.current_milestone);
  return <div className="grid grid-cols-8 items-center gap-1" role="img" aria-label={`Recorded stage: ${milestoneLabel(project.current_milestone)}. Other cells do not indicate completion.`}>
    {REPORT_STAGES.map((stage, index) => <span key={stage.value} aria-hidden="true" title={milestoneLabel(stage.value)}
      data-current-stage={stageIndex === index ? stage.value : undefined}
      className={stageIndex === index
        ? "flex h-8 min-w-0 items-center justify-center rounded-md bg-primary text-xs font-semibold text-primary-foreground"
        : "flex h-8 min-w-0 items-center justify-center rounded-md bg-muted/45 text-xs text-muted-foreground"}>
      {labelled || stageIndex === index ? stage.short : <span className="size-1 rounded-full bg-muted-foreground/35" />}
    </span>)}
  </div>;
}

function OwnerLabel({ value }: { value: string }) {
  return <Badge variant="outline" className="rounded-md bg-muted/35">{value}</Badge>;
}

export function ProjectMilestoneOverview({ report, svgRef }: { report: ProjectReport; svgRef: Ref<SVGSVGElement> }) {
  const [selected, setSelected] = useState<ReportProject | null>(null);
  const [guide, setGuide] = useState(false);
  const items = orderedActiveProjects(report.active);
  return <div data-testid="active-report-surface" className="space-y-3">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <div className="flex flex-wrap items-center gap-3">
        <h2 id="active-title" className="text-lg font-semibold tracking-tight">Active projects <span className="ml-1 text-sm font-normal text-muted-foreground">{items.length}</span></h2>
        <span className="hidden text-xs text-muted-foreground sm:inline">Later stages first</span>
      </div>
      <Button variant="ghost" size="sm" className="text-muted-foreground" onClick={() => setGuide(true)}><Info className="size-4" />Stage guide</Button>
    </div>
    {!items.length ? <p className="rounded-xl border bg-card p-6 text-sm text-muted-foreground">No active projects in this snapshot.</p> : <>
      <div className="hidden overflow-hidden rounded-xl border bg-card xl:block" data-testid="active-desktop">
        <Table className="w-full table-fixed" aria-label="Active milestone overview">
          <TableHeader className="bg-muted/40"><TableRow>
            <TableHead className="w-[22%] px-4 py-3 text-xs text-muted-foreground">Project</TableHead>
            <TableHead className="w-[29%] px-3 py-3" aria-label="Current milestone, later stages first" aria-sort="descending"><div className="grid grid-cols-8 gap-1">{REPORT_STAGES.map(stage => <span key={stage.value} className="text-center text-xs font-medium text-muted-foreground" title={milestoneLabel(stage.value)}>{stage.short}</span>)}</div></TableHead>
            <TableHead className="w-[37%] px-4 py-3 text-xs text-muted-foreground">Recorded next action</TableHead>
            <TableHead className="w-[12%] px-3 py-3 text-xs text-muted-foreground">Ball with</TableHead>
          </TableRow></TableHeader>
          <TableBody>{items.map(project => <TableRow key={project.project_id} data-project-id={project.project_id}>
            <TableCell className="whitespace-normal px-4 py-3 align-top">
              <button type="button" onClick={() => setSelected(project)} aria-label={`View report details for ${project.project_id}`}
                className="-my-1 flex min-h-11 items-center gap-2 rounded text-left text-sm font-semibold leading-5 hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
                <span className="min-w-0 break-words">{project.client_name || project.project_id}</span><ChevronRight className="size-3.5 shrink-0 text-muted-foreground" />
              </button>
              <p className="mt-1 text-xs leading-5 text-muted-foreground">{project.project_id} / {project.market || "Market not recorded"}</p>
              {project.project_status === "Blocked" && <span className="mt-1 inline-block text-xs font-medium text-danger">Blocked</span>}
            </TableCell>
            <TableCell className="px-3 py-4"><StageCells project={project} />{milestoneIndex(project.current_milestone) < 0 && <p className="mt-1 text-xs text-muted-foreground">Stage not recorded</p>}</TableCell>
            <TableCell className="whitespace-normal px-4 py-4 align-top"><p className="line-clamp-3 break-words text-sm leading-6">{project.next_action || "Next action not recorded. Confirm with the project owner."}</p></TableCell>
            <TableCell className="px-3 py-4 align-top"><OwnerLabel value={project.ball_with} /></TableCell>
          </TableRow>)}</TableBody>
        </Table>
      </div>
      <div className="grid gap-3 md:grid-cols-2 xl:hidden" data-testid="active-cards">
        {items.map(project => <article key={project.project_id} data-card-project-id={project.project_id} className="min-w-0 space-y-3 rounded-xl border bg-card p-4">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0"><h3 className="break-words text-sm font-semibold leading-6">{project.client_name || project.project_id}</h3><p className="text-xs leading-5 text-muted-foreground">{project.project_id} / {project.market || "Market not recorded"}</p></div>
            <OwnerLabel value={project.ball_with} />
          </div>
          <StageCells project={project} labelled />
          <p className="text-xs leading-5 text-muted-foreground">{milestoneLabel(project.current_milestone)}{project.project_status === "Blocked" ? " / Blocked" : ""}</p>
          <p className="line-clamp-3 break-words text-sm leading-6">{project.next_action || "Next action not recorded. Confirm with the project owner."}</p>
          <Button variant="outline" size="sm" className="w-full" onClick={() => setSelected(project)} aria-label={`View report details for ${project.project_id}`}>View details <ChevronRight className="size-3.5" /></Button>
        </article>)}
      </div>
    </>}
    <p className="text-xs leading-5 text-muted-foreground">Only the current stage is marked. Open a project for its full action and secondary dates.</p>
    <div hidden aria-hidden="true" data-testid="overview-export-source"><ProjectOverviewImage report={report} items={items} svgRef={svgRef} /></div>
    <Dialog open={!!selected || guide} onOpenChange={open => { if (!open) { setSelected(null); setGuide(false); } }}>
      <DialogContent className="max-h-[90vh] max-w-3xl overflow-y-auto">
        <DialogHeader><DialogTitle>{guide ? "Milestone guide" : selected?.client_name || selected?.project_id}</DialogTitle><DialogDescription>{guide ? "Recorded current stages, not completion percentages. Empty cells make no claim about past or future completion." : "Full details from the displayed report snapshot."}</DialogDescription></DialogHeader>
        {guide ? <ol className="mt-4 divide-y">{REPORT_STAGES.map(stage => <li key={stage.value} className="flex items-center gap-4 py-3 text-sm"><span className="rounded-md bg-muted px-2 py-1 font-semibold">{stage.short}</span>{stage.label}</li>)}</ol> : selected && <div className="mt-5 space-y-5">
          <dl className="grid grid-cols-2 gap-4 text-sm">
            <div><dt className="text-muted-foreground">Project</dt><dd>{selected.project_id}</dd></div>
            <div><dt className="text-muted-foreground">Market</dt><dd>{selected.market || "Not recorded"}</dd></div>
            <div><dt className="text-muted-foreground">Status</dt><dd>{selected.project_status}</dd></div>
            <div><dt className="text-muted-foreground">Ball with</dt><dd>{selected.ball_with}</dd></div>
            <div className="col-span-2"><dt className="text-muted-foreground">Recorded current stage</dt><dd>{milestoneLabel(selected.current_milestone)}</dd></div>
          </dl>
          <div><h3 className="text-sm font-semibold">Recorded next action</h3><p className="mt-2 whitespace-pre-wrap break-words text-sm leading-6">{selected.next_action || "Not recorded"}</p></div>
          <div className="rounded-lg border bg-muted/30 p-4 text-sm"><h3 className="font-semibold">Secondary dates</h3><p className="mt-2">{targetLabel(selected, report.report_date)}</p><p className="mt-1 text-muted-foreground">Tracked since: {selected.tracked_since || "Not recorded"} (system record date, not project start)</p></div>
          {(selected.data_notes ?? []).map(note => <p key={note} className="text-sm text-muted-foreground">{note}</p>)}
          <Link className={buttonVariants({ variant: "outline" })} href={`/projects/${encodeURIComponent(selected.project_id)}`}>Open project workspace</Link>
        </div>}
      </DialogContent>
    </Dialog>
  </div>;
}
