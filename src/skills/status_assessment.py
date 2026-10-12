"""
Status Assessment Skill — deterministic health check from project truth fields.
No LLM. Returns SkillBrief with observations and recommendations.
"""

import sqlite3
from datetime import datetime, timezone

from src.models.enums import BallWith, ProjectStatus
from src.models.project import Project
from src.skills.fix_onboarding import SkillBrief


class StatusAssessmentSkill:
    """Assess project health from truth fields. Deterministic, no LLM."""

    skill_type = "status_assessment"

    def assess(
        self,
        project: Project,
        conn: sqlite3.Connection | None = None,
        *,
        now: datetime | None = None,
    ) -> SkillBrief:
        """
        Assess project health from truth fields.
        Returns SkillBrief with observations (current state) and
        recommendations (actions the operator should take).
        """
        now = now or datetime.now(timezone.utc)
        observations: list[str] = []
        recommendations: list[str] = []
        caveats: list[str] = []

        # ── Status ───────────────────────────────────────────────────────────
        if project.project_status == ProjectStatus.ACTIVE:
            observations.append("Project is active")
        elif project.project_status == ProjectStatus.BLOCKED:
            observations.append("Project is blocked — action required to unblock")
            recommendations.append("Identify and resolve blocking issue; escalate if needed")
        elif project.project_status == ProjectStatus.WAITING:
            observations.append("Project is waiting (external dependency)")
            recommendations.append("Confirm what is awaited and set a follow-up date")
        elif project.project_status == ProjectStatus.COMPLETED:
            observations.append("Project status is Completed")
        elif project.project_status in (ProjectStatus.ARCHIVED, ProjectStatus.CANCELLED):
            observations.append(f"Project is {project.project_status.value} — no active work expected")
        else:
            observations.append(f"Project status: {project.project_status.value}")

        # ── Ball-with ─────────────────────────────────────────────────────────
        if project.ball_with == BallWith.INTERNAL:
            observations.append("Ball is with Internal team")
            if not project.next_action:
                observations.append("No next_action recorded despite internal ownership")
                recommendations.append("Record a next_action to track internal responsibility")
        elif project.ball_with == BallWith.EXTERNAL:
            observations.append("Ball is with External counterparty — awaiting their response")
            if not project.next_action:
                recommendations.append("Consider scheduling a follow-up: no next_action recorded")
        elif project.ball_with == BallWith.UNKNOWN:
            observations.append("Ball-with is Unknown — responsibility gap")
            recommendations.append("Set ball_with to Internal or External to clear ownership")

        # ── Milestone ─────────────────────────────────────────────────────────
        if project.current_milestone:
            observations.append(f"Current milestone: {project.current_milestone.value}")
        else:
            observations.append("No current milestone recorded")
            recommendations.append("Set current_milestone to track onboarding stage")
            caveats.append("milestone is None — assessment may be incomplete")

        # ── Deadline ──────────────────────────────────────────────────────────
        if project.target_go_live:
            try:
                target = datetime.fromisoformat(
                    project.target_go_live.replace("Z", "+00:00")
                ).replace(tzinfo=timezone.utc)
                days_left = (target - now.replace(tzinfo=timezone.utc)).days
                if days_left < 0:
                    observations.append(f"Go-live overdue by {abs(days_left)} day(s)")
                    recommendations.append("Escalate go-live overrun immediately")
                elif days_left <= 2:
                    observations.append(f"Go-live imminent: {days_left} day(s) remaining")
                    recommendations.append("Confirm all outstanding items are resolved")
                elif days_left <= 7:
                    observations.append(f"Go-live in {days_left} day(s)")
                    recommendations.append("Review readiness checklist")
                else:
                    observations.append(f"Go-live in {days_left} day(s) — on schedule")
            except (ValueError, AttributeError):
                caveats.append(f"Cannot parse target_go_live: {project.target_go_live!r}")
        else:
            caveats.append("No target_go_live recorded")

        # ── Open follow-ups (requires DB) ─────────────────────────────────────
        if conn is not None:
            try:
                row = conn.execute(
                    "SELECT COUNT(*) FROM internal_followups "
                    "WHERE project_id = ? AND status IN ('open','overdue')",
                    (project.project_id,),
                ).fetchone()
                open_count = row[0] if row else 0
                if open_count > 0:
                    observations.append(f"{open_count} open or overdue follow-up(s)")
                    recommendations.append("Resolve or reassign open follow-ups")
            except Exception:
                caveats.append("Could not query follow-ups")

        # ── Next action ───────────────────────────────────────────────────────
        if project.next_action:
            observations.append(f"Next action: {project.next_action}")

        if not recommendations:
            recommendations.append("Project appears on track — no immediate actions required")

        return SkillBrief(
            skill_type=self.skill_type,
            observations=observations,
            recommendations=recommendations,
            confidence=1.0,
            caveats=caveats,
        )
