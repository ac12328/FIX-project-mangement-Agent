import type { components } from "@/lib/generated/api-schema";
import { MILESTONE_SHORT } from "@/lib/milestones";

export type ProjectReport = components["schemas"]["ProjectSummaryReport"];
export type ReportProject = components["schemas"]["ProjectReportItem"];
export const REPORT_EVENTS = ["project_created", "project_updated", "project_email_processed", "review_approved", "followup_completed"];
export const REPORT_STAGES = Object.entries(MILESTONE_SHORT).map(([value, stage]) => ({ value, ...stage }));
export const milestoneIndex = (value?: string | null) => REPORT_STAGES.findIndex(stage => stage.value === value);

/** Later recorded stages first; never mutate the API's frozen snapshot. */
export function orderedActiveProjects(items: ReportProject[]) {
  return [...items].sort((a, b) => milestoneIndex(b.current_milestone) - milestoneIndex(a.current_milestone)
    || Number(b.project_status === "Blocked") - Number(a.project_status === "Blocked")
    || (a.client_name || a.project_id).localeCompare(b.client_name || b.project_id)
    || a.project_id.localeCompare(b.project_id));
}

/** Layout-only wrapping. Ellipses disclose truncation; Markdown retains full text. */
export function reportTextLines(value: string, maxUnits: number, maxLines: number) {
  const text = value.replace(/\s+/g, " ").trim();
  const characters = Array.from(text);
  const lines: string[] = [];
  let line = "", units = 0;
  for (let index = 0; index < characters.length; index++) {
    const character = characters[index];
    const width = /[^\u0000-\u00ff]/.test(character) ? 2 : 1;
    if (units + width > maxUnits - 1) {
      // Prefer word boundaries, but do not discard or reorder text.
      const space = line.lastIndexOf(" ");
      if (lines.length === maxLines - 1) {
        lines.push(line.trimEnd() + "\u2026");
        return lines;
      }
      if (space > line.length / 2 && character !== " ") {
        const carry = line.slice(space + 1);
        lines.push(line.slice(0, space));
        line = carry;
        units = Array.from(carry).reduce((n, char) => n + (/[^\u0000-\u00ff]/.test(char) ? 2 : 1), 0);
      } else {
        lines.push(line.trimEnd()); line = ""; units = 0;
      }
    }
    if (!line && character === " ") continue;
    line += character; units += width;
  }
  if (line) lines.push(line.trimEnd());
  return lines;
}

export function milestoneLabel(value?: string | null) {
  if (!value) return "Not recorded";
  const label = MILESTONE_SHORT[value];
  return label ? `${label.short} - ${label.label}` : value;
}
export function formatReportSnapshot(value: string) {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.valueOf())) return "Snapshot time unavailable";
  return new Intl.DateTimeFormat("en-GB", { timeZone: "Asia/Hong_Kong", dateStyle: "medium", timeStyle: "short" }).format(parsed) + " HKT";
}

export function completedDate(value?: string | null) {
  return value ? new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Hong_Kong", year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date(value)) : "Not recorded";
}
export function targetLabel(project: ReportProject, reportDate: string) {
  if (!project.target_go_live) return "Target TBD";
  if (project.project_status === "Waiting") return `Previous target: ${project.target_go_live}`;
  return project.target_go_live < reportDate
    ? `Target passed: ${project.target_go_live} - confirm revised date`
    : `Target: ${project.target_go_live}`;
}
// Escape source text so notes cannot become raw HTML or extra Markdown rows.
export const markdownCell = (value: unknown) => String(value ?? "Not recorded")
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  .replace(/\\/g, "\\\\").replace(/[|`*_\[\]]/g, "\\$&").replace(/[\r\n]+/g, " ");
function table(headers: string[], rows: unknown[][]) {
  if (!rows.length) return "None in this snapshot.";
  return [headers, headers.map(() => "---"), ...rows].map(row => `| ${row.map(markdownCell).join(" | ")} |`).join("\n");
}
export function projectReportMarkdown(report: ProjectReport) {
  const projectName = (p: ReportProject) => `${p.client_name || p.project_id} (${p.project_id})`;
  return [
    "# Project Summary",
    `Report date: ${report.report_date} (${report.timezone})  \nSnapshot: ${report.generated_at}  \nScope: ${report.scope === "mine" ? "My projects" : "All shared projects"}`,
    `Active: ${report.active.length} / Postponed: ${report.postponed.length} / Completed in window: ${report.completed.length}`,
    ...(report.warnings ?? []).map(warning => `> Data note: ${markdownCell(warning)}`),
    "## 1. Active Projects",
    "Paste the copied milestone overview image here. Copy and paste the Markdown before copying the image.",
    "Each project has one marker at its recorded current milestone. Other cells make no claim about completed stages. This is not a schedule or percentage complete. Projects are ordered by recorded stage, later stages first.",
    table(["Project", "Market", "Status", "Current stage", "Ball with", "Recorded next action", "Data notes"], orderedActiveProjects(report.active).map(p => [projectName(p), p.market, p.project_status, milestoneLabel(p.current_milestone), p.ball_with, p.next_action, (p.data_notes ?? []).join("; ") || "None"])),
    "## 2. Postponed Projects",
    "Waiting means an intentional pause. Previous targets are not current commitments. Pause reasons and restart conditions are not inferred from free-text notes.",
    table(["Project", "Market", "Current stage", "Previous target", "Ball with", "Recorded next action", "Pause reason / restart date", "Data notes"], report.postponed.map(p => [projectName(p), p.market, milestoneLabel(p.current_milestone), p.target_go_live || "TBD", p.ball_with, p.next_action, "Not recorded in dedicated fields", (p.data_notes ?? []).join("; ") || "None"])),
    "## 3. Completed - Last 14 Days",
    `${report.completed_from} to ${report.completed_through}, inclusive (${report.timezone}), through snapshot time. Accepted completion date, not actual go-live date.`,
    table(["Project", "Market", "Current stage", "Completed on"], report.completed.map(p => [projectName(p), p.market, milestoneLabel(p.current_milestone), completedDate(p.completed_at)])),
    "Internal, Archived and Cancelled projects are excluded. Only currently Completed projects with valid accepted completion audits appear in the completion window.",
  ].join("\n\n") + "\n";
}
