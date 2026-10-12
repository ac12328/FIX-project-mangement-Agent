import type { Ref } from "react";
import { REPORT_STAGES, formatReportSnapshot, milestoneIndex, milestoneLabel, reportTextLines, type ProjectReport, type ReportProject } from "@/lib/project-report";

const palette = {
  background: "var(--card)", text: "var(--foreground)", muted: "var(--muted-foreground)",
  grid: "var(--border)", subtle: "var(--muted)", primary: "var(--primary)",
  onPrimary: "var(--primary-foreground)", danger: "var(--danger)",
};
const headerLabels = [["Network"], ["Session"], ["UAT access"], ["Hub setup"], ["Conformance"], ["Report"], ["Prod / PCT"], ["Go-live"]];

/** Clipboard layout only; shares the ordered, frozen items with the readable UI. */
export function ProjectOverviewImage({ report, items, svgRef }: { report: ProjectReport; items: ReportProject[]; svgRef: Ref<SVGSVGElement> }) {
  const width = 1600, top = 166, rowHeight = 86, left = 300, cell = 52, actionX = 748, ownerX = 1440;
  const height = top + Math.max(1, items.length) * rowHeight + 46;
  const caption = `Milestone overview, ${report.report_date}, ${items.length} active projects. One marker per recorded stage; not percentage complete.`;
  return (
      <svg ref={svgRef} xmlns="http://www.w3.org/2000/svg" viewBox={`0 0 ${width} ${height}`} width={width} height={height}
        role="img" aria-label={caption} style={{ fontFamily: "Arial, sans-serif", fontSize: 13 }}>
        <title>Active project milestone overview</title>
        <desc>Only the recorded current stage is highlighted. Empty cells are not a claim about earlier or future stage completion. Full next actions are also in the Markdown export.</desc>
        <rect width={width} height={height} fill={palette.background} />
        <rect width={width} height={4} fill={palette.primary} />
        <text x={28} y={34} fill={palette.primary} fontSize={11} fontWeight={700} letterSpacing={1.3}>PORTFOLIO REPORT / ACTIVE PROJECTS</text>
        <text x={28} y={58} fill={palette.muted} fontSize={13}>{report.report_date} HKT / {items.length} active projects / {report.scope === "mine" ? "My projects" : "All shared projects"}</text>
        <text x={width - 28} y={34} textAnchor="end" fill={palette.muted} fontSize={11}>{formatReportSnapshot(report.generated_at)}</text>
        <circle cx={width - 270} cy={54} r={6} fill={palette.primary} />
        <text x={width - 256} y={58} fill={palette.muted} fontSize={12}>Recorded current stage only</text>
        <text x={28} y={82} fill={palette.muted} fontSize={12}>Later stages first. One recorded stage per project. Full actions are retained in the Markdown report.</text>
        <rect x={20} y={100} width={width - 40} height={top - 100} fill={palette.subtle} />
        {[
          { from: 0, span: 3, label: "NETWORK & ACCESS" },
          { from: 3, span: 3, label: "SETUP & TESTING" },
          { from: 6, span: 2, label: "PRODUCTION" },
        ].map(group => <g key={group.label}>
          <text x={left + (group.from + group.span / 2) * cell} y={115} fill={palette.muted} textAnchor="middle" fontSize={10} letterSpacing={0.5}>{group.label}</text>
          <line x1={left + group.from * cell + 5} x2={left + (group.from + group.span) * cell - 5} y1={121} y2={121} stroke={palette.grid} />
        </g>)}
        <text x={32} y={140} fill={palette.muted} fontWeight={600} fontSize={11}>PROJECT / CURRENT STAGE</text>
        <text x={actionX} y={140} fill={palette.muted} fontWeight={600} fontSize={11}>RECORDED NEXT ACTION</text>
        <text x={ownerX} y={140} fill={palette.muted} fontWeight={600} fontSize={11}>BALL WITH</text>
        {REPORT_STAGES.map((stage, index) => <g key={stage.value}>
          <title>{milestoneLabel(stage.value)}</title>
          <text x={left + (index + 0.5) * cell} y={138} textAnchor="middle" fill={palette.text} fontSize={12} fontWeight={700}>{stage.short}</text>
          {headerLabels[index].map((line, i) => <text key={i} x={left + (index + 0.5) * cell} y={155 + i * 11} textAnchor="middle" fill={palette.muted} fontSize={9}>{line}</text>)}
          <line x1={left + index * cell} x2={left + index * cell} y1={top} y2={height - 46} stroke={palette.grid} strokeOpacity={0.6} />
        </g>)}
        <line x1={left + 8 * cell} x2={left + 8 * cell} y1={top} y2={height - 46} stroke={palette.grid} />
        {!items.length && <text x={32} y={top + 44} fill={palette.muted}>No active projects in this snapshot.</text>}
        {items.map((project, index) => {
          const y = top + index * rowHeight, stageIndex = milestoneIndex(project.current_milestone);
          const blocked = project.project_status === "Blocked";
          const nextAction = project.next_action?.trim() || "Next action not recorded. Confirm with the project owner.";
          return <g key={project.project_id} data-image-project-id={project.project_id}>
            <title>{`${project.client_name || project.project_id}: ${milestoneLabel(project.current_milestone)}. ${project.project_status}. Ball with ${project.ball_with}. ${nextAction}`}</title>
            <rect x={21} y={y + 1} width={width - 42} height={rowHeight - 2} fill="transparent" stroke="transparent" strokeWidth={2}
              />
            <line x1={20} x2={width - 20} y1={y} y2={y} stroke={palette.grid} />
            <text x={32} y={y + 26} fill={palette.text} fontSize={15} fontWeight={700}>{reportTextLines(project.client_name || project.project_id, 29, 1)[0]}</text>
            <text x={32} y={y + 46} fill={blocked ? palette.danger : palette.muted} fontSize={11}>{reportTextLines(`${project.project_id} / ${project.market || "Market not recorded"}${blocked ? " / Blocked" : ""}`, 43, 1)[0]}</text>
            <text x={32} y={y + 66} fill={palette.muted} fontSize={11}>{reportTextLines(milestoneLabel(project.current_milestone), 43, 1)[0]}</text>
            {REPORT_STAGES.map((stage, stageNumber) => {
              const current = stageNumber === stageIndex;
              const cx = left + (stageNumber + 0.5) * cell, cy = y + 40;
              return current ? <g key={stage.value} data-image-current-stage={stage.value}>
                <rect x={cx - 20} y={cy - 18} width={40} height={36} rx={9} fill={palette.primary} />
                <text x={cx} y={cy + 5} textAnchor="middle" fill={palette.onPrimary} fontSize={14} fontWeight={700}>{stage.short}</text>
              </g> : <circle key={stage.value} cx={cx} cy={cy} r={3} fill={palette.grid} />;
            })}
            {stageIndex < 0 && <text x={left + 8} y={y + 74} fill={palette.muted} fontSize={10}>Stage not recorded / unrecognized</text>}
            {reportTextLines(nextAction, 90, 3).map((line, lineIndex) => <text key={lineIndex} x={actionX} y={y + 25 + lineIndex * 18} fill={palette.text} fontSize={13}>{line}</text>)}
            <text x={ownerX} y={y + 44} fill={palette.text} fontSize={13} fontWeight={600}>{project.ball_with}</text>
          </g>;
        })}
        <line x1={20} x2={width - 20} y1={height - 46} y2={height - 46} stroke={palette.grid} />
        <text x={28} y={height - 27} fill={palette.muted} fontSize={11}>Filled cell = recorded current stage. Other cells are not completion evidence. This is not a schedule or percentage complete.</text>
        <text x={28} y={height - 10} fill={palette.muted} fontSize={11}>Actions are verbatim excerpts, not generated summaries. Ellipses indicate longer text; the complete actions remain in the Markdown report.</text>
      </svg>
  );
}
