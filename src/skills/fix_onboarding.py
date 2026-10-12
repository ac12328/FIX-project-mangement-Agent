"""
FIX Onboarding Skill — domain knowledge about FIX protocol onboarding stages.
"""

import logging
import sqlite3
from dataclasses import dataclass, field

from src.advisory.milestone_wiki import get_milestone_guide
from src.models.enums import Milestone

logger = logging.getLogger(__name__)


@dataclass
class SkillBrief:
    """Output of a Skill invocation."""
    skill_type: str
    observations: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)
    confidence: float = 1.0
    caveats: list[str] = field(default_factory=list)


def get_learned_durations(conn: sqlite3.Connection, min_samples: int = 3) -> dict[str, float]:
    """Compute average days per milestone from actual milestone_history transitions.

    Groups consecutive transitions per project to measure how long each milestone
    took. Returns only milestones with >= min_samples data points.
    """
    try:
        rows = conn.execute(
            """SELECT project_id, to_milestone, changed_at
               FROM milestone_history
               ORDER BY project_id, changed_at ASC"""
        ).fetchall()
    except Exception:
        logger.debug("Failed to query milestone_history", exc_info=True)
        return {}

    if not rows:
        return {}

    from datetime import datetime

    # Group transitions by project, compute duration between consecutive transitions
    durations: dict[str, list[float]] = {}
    prev_project = None
    prev_at = None

    for row in rows:
        pid = row["project_id"] if isinstance(row, sqlite3.Row) else row[0]
        milestone = row["to_milestone"] if isinstance(row, sqlite3.Row) else row[1]
        changed_at = row["changed_at"] if isinstance(row, sqlite3.Row) else row[2]

        try:
            current_at = datetime.fromisoformat(changed_at)
        except (ValueError, TypeError):
            prev_project, prev_at = pid, None
            continue

        if pid == prev_project and prev_at is not None:
            delta_days = (current_at - prev_at).total_seconds() / 86400
            if 0 < delta_days < 365:  # sanity bound
                durations.setdefault(milestone, []).append(delta_days)

        prev_project = pid
        prev_at = current_at

    # Average only milestones with enough samples
    result: dict[str, float] = {}
    for milestone, days_list in durations.items():
        if len(days_list) >= min_samples:
            result[milestone] = round(sum(days_list) / len(days_list), 1)

    return result


class FIXOnboardingSkill:
    """Provides FIX onboarding domain knowledge."""

    skill_type = "fix_onboarding"

    def assess(
        self,
        current_milestone: str | None,
        ball_with: str | None,
        days_since_last_activity: int | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> SkillBrief:
        """Assess the current project state and provide observations."""
        observations = []
        recommendations = []
        caveats = []

        if not current_milestone:
            return SkillBrief(
                skill_type=self.skill_type,
                observations=["No milestone set — project may be newly created"],
                recommendations=["Set initial milestone to M1_NetworkSetup"],
                confidence=0.5,
            )

        knowledge = get_milestone_guide(current_milestone)
        if not knowledge:
            caveats.append(f"Unknown milestone: {current_milestone}")
            return SkillBrief(
                skill_type=self.skill_type,
                caveats=caveats,
                confidence=0.3,
            )

        # Use learned durations if available, fall back to static
        typical_days = knowledge.get("typical_duration_days", 5)
        if conn is not None:
            learned = get_learned_durations(conn)
            if current_milestone in learned:
                typical_days = learned[current_milestone]
                observations.append(
                    f"Learned avg duration for {current_milestone}: {typical_days:.1f} days "
                    f"(static default: {knowledge.get('typical_duration_days', 5)})"
                )
        if days_since_last_activity is not None:
            if days_since_last_activity > typical_days * 2:
                observations.append(
                    f"Project appears stalled — {days_since_last_activity} days "
                    f"since last activity (typical: {typical_days} days)"
                )
                recommendations.append("Follow up with the responsible party")
            elif days_since_last_activity > typical_days:
                observations.append(
                    f"Project is taking longer than typical "
                    f"({days_since_last_activity} vs {typical_days} days)"
                )

        # Ball assignment check
        expected_ball = knowledge.get("ball_usually_with")
        if ball_with and expected_ball and ball_with != expected_ball:
            observations.append(
                f"Ball is with {ball_with} but typically with {expected_ball} at this stage"
            )

        # Add typical blockers as observations
        observations.append(f"Typical blockers at {current_milestone}: "
                          + ", ".join(knowledge.get("typical_blockers", [])))

        recommendations.extend(knowledge.get("key_actions", []))

        return SkillBrief(
            skill_type=self.skill_type,
            observations=observations,
            recommendations=recommendations,
            confidence=0.85,
        )
