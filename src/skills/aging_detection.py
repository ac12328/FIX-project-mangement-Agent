"""
Aging Detection Skill — canonical aging thresholds for projects, SLAs, and follow-ups.
Consolidates logic from morning_brief, sla_monitor, inactivity_alert, project_closing.
No LLM. Deterministic. Returns SkillBrief.
"""

import sqlite3
from datetime import datetime, timezone

from src.models.enums import BallWith
from src.models.project import Project
from src.skills.fix_onboarding import SkillBrief
from src.utils.calendar import INACTIVITY_THRESHOLD_DAYS, business_days_between


# ── Canonical aging thresholds (single source of truth for all modules) ──────

# External ball-with aging (calendar days since last project activity)
EXTERNAL_AGING_YELLOW_DAYS = 3   # morning_brief: +10 pts; consider follow-up
EXTERNAL_AGING_RED_DAYS = 7      # morning_brief: +25 pts; escalate

# Project closing suggestion (business days of inactivity after Completed)
CLOSING_SUGGESTION_BIZ_DAYS = 7  # triggers closing_suggestions scheduler job

# Operator inactivity alert (business days without Telegram interaction)
OPERATOR_INACTIVITY_BIZ_DAYS = INACTIVITY_THRESHOLD_DAYS  # 3 biz days (Section 34.1)

# SLA alert levels (calendar days remaining to due date)
SLA_ADVISORY_DAYS = 3    # alert_level: advisory
SLA_URGENT_DAYS = 1      # alert_level: urgent
SLA_DUE_TODAY_DAYS = 0   # alert_level: due_today


class AgingDetectionSkill:
    """Centralized aging detection for projects, SLAs, and follow-ups."""

    skill_type = "aging_detection"

    def classify_project_aging(
        self,
        project: Project,
        days_inactive: int | None,
    ) -> SkillBrief:
        """
        Classify a project's aging state.
        days_inactive: calendar days since last recorded project activity.
        """
        if days_inactive is None:
            return SkillBrief(
                skill_type=self.skill_type,
                observations=["No last-activity date recorded — aging cannot be assessed"],
                recommendations=["Ensure activity is being logged for this project"],
                confidence=1.0,
            )

        observations: list[str] = []
        recommendations: list[str] = []

        if project.ball_with == BallWith.EXTERNAL:
            if days_inactive >= EXTERNAL_AGING_RED_DAYS:
                observations.append(
                    f"External counterparty has not responded for {days_inactive} day(s) — RED (≥{EXTERNAL_AGING_RED_DAYS}d)"
                )
                recommendations.append("Send follow-up or escalate to senior contact")
            elif days_inactive >= EXTERNAL_AGING_YELLOW_DAYS:
                observations.append(
                    f"External counterparty aging: {days_inactive} day(s) — YELLOW (≥{EXTERNAL_AGING_YELLOW_DAYS}d)"
                )
                recommendations.append("Send a gentle reminder")
            else:
                observations.append(
                    f"External counterparty aging within threshold: {days_inactive} day(s)"
                )
        else:
            if days_inactive >= EXTERNAL_AGING_RED_DAYS:
                observations.append(
                    f"Project inactive for {days_inactive} day(s) — review required"
                )
                recommendations.append("Verify project status and update next_action")
            else:
                observations.append(
                    f"Project activity is current: {days_inactive} day(s) since last change"
                )

        return SkillBrief(
            skill_type=self.skill_type,
            observations=observations,
            recommendations=recommendations,
            confidence=1.0,
        )

    def classify_sla_urgency(self, days_remaining: int) -> SkillBrief:
        """
        Classify SLA urgency using canonical thresholds.
        Mirrors sla_monitor._classify_alert() logic.
        """
        if days_remaining < 0:
            level, obs, rec = (
                "overdue",
                f"SLA overdue by {abs(days_remaining)} day(s)",
                "Escalate immediately — SLA breach",
            )
        elif days_remaining == SLA_DUE_TODAY_DAYS:
            level, obs, rec = "due_today", "SLA due today", "Complete this item today"
        elif days_remaining <= SLA_URGENT_DAYS:
            level, obs, rec = (
                "urgent",
                f"SLA due in {days_remaining} day(s) — urgent",
                "Prioritise and act today",
            )
        elif days_remaining <= SLA_ADVISORY_DAYS:
            level, obs, rec = (
                "advisory",
                f"SLA due in {days_remaining} day(s) — advisory",
                "Plan to complete within the next 1–2 days",
            )
        else:
            level, obs, rec = (
                "ok",
                f"SLA due in {days_remaining} day(s) — within threshold",
                "No immediate action required",
            )

        return SkillBrief(
            skill_type=self.skill_type,
            observations=[obs, f"Alert level: {level}"],
            recommendations=[rec],
            confidence=1.0,
        )

    def is_closing_candidate(
        self,
        project: Project,
        conn: sqlite3.Connection,
        now: datetime | None = None,
    ) -> SkillBrief:
        """
        Check if a Completed project qualifies for formal closing.
        Uses CLOSING_SUGGESTION_BIZ_DAYS threshold — mirrors project_closing._check_closing_candidates().
        """
        now = now or datetime.now(timezone.utc)

        if project.project_status.value != "Completed":
            return SkillBrief(
                skill_type=self.skill_type,
                observations=[
                    f"Project status is {project.project_status.value} — not a closing candidate"
                ],
                recommendations=[],
                confidence=1.0,
            )

        try:
            row = conn.execute(
                "SELECT MAX(created_at) FROM activity_log WHERE project_id = ?",
                (project.project_id,),
            ).fetchone()
            last_at_str = row[0] if row else None
            if not last_at_str:
                return SkillBrief(
                    skill_type=self.skill_type,
                    observations=["No activity recorded — treating as closing candidate"],
                    recommendations=["Initiate formal closing checklist"],
                    confidence=1.0,
                )
            last_at = datetime.fromisoformat(last_at_str.replace("Z", "+00:00"))
            biz_days = business_days_between(last_at, now)
            if biz_days >= CLOSING_SUGGESTION_BIZ_DAYS:
                return SkillBrief(
                    skill_type=self.skill_type,
                    observations=[
                        f"Project inactive for {biz_days} business day(s) — closing candidate"
                    ],
                    recommendations=["Initiate formal closing checklist"],
                    confidence=1.0,
                )
            return SkillBrief(
                skill_type=self.skill_type,
                observations=[
                    f"Project active within {biz_days} business day(s) — "
                    f"closing threshold is {CLOSING_SUGGESTION_BIZ_DAYS} biz days"
                ],
                recommendations=[],
                confidence=1.0,
            )
        except Exception:
            pass

        return SkillBrief(
            skill_type=self.skill_type,
            observations=["Could not assess closing candidacy — activity log unavailable"],
            recommendations=["Verify project status"],
            confidence=1.0,
        )
