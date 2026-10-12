"""
Communication Skill — drafting best practices and tone management.
"""

from src.skills.fix_onboarding import SkillBrief


# Internal jargon that should never appear in client emails
INTERNAL_JARGON = [
    "M1", "M2", "M3", "M4", "M5", "M6", "M7", "M8",
    "milestone", "ProposedUpdate", "Write Layer", "Guard",
    "ball_with", "next_action", "evidence_store",
    "triage", "Lead Agent", "Skill Layer",
    "Demo Gateway A", "Production Preparation", "Demo Connectivity",
]

# Client-safe alternatives
JARGON_REPLACEMENTS = {
    "M1": "network setup phase",
    "M2": "session configuration phase",
    "M3": "UAT access phase",
    "M4": "counterparty setup phase",
    "M5": "conformance testing phase",
    "M6": "test report phase",
    "M7": "production preparation phase",
    "M8": "go-live phase",
}


class CommunicationSkill:
    """Provides communication tone and drafting guidance."""

    skill_type = "communication"

    def check_draft(
        self,
        draft_text: str,
        avoid_phrases: list[str] | None = None,
    ) -> SkillBrief:
        """
        Check a draft for internal jargon and avoid_phrases violations.
        Returns warnings and suggestions.
        """
        observations = []
        recommendations = []
        text_lower = draft_text.lower()

        # Check for internal jargon
        for jargon in INTERNAL_JARGON:
            if jargon.lower() in text_lower:
                replacement = JARGON_REPLACEMENTS.get(jargon)
                if replacement:
                    observations.append(f"Internal jargon '{jargon}' found — replace with '{replacement}'")
                else:
                    observations.append(f"Internal jargon '{jargon}' found — remove or rephrase")

        # Check operator-defined avoid_phrases
        for phrase in (avoid_phrases or []):
            if phrase.lower() in text_lower:
                observations.append(f"Avoid phrase '{phrase}' detected — must be removed")

        if not observations:
            observations.append("Draft passes jargon and avoid-phrase checks")

        return SkillBrief(
            skill_type=self.skill_type,
            observations=observations,
            recommendations=recommendations,
            confidence=1.0,  # Deterministic check
        )

    def suggest_tone(
        self,
        preferred_tone: str | None = None,
        is_follow_up: bool = False,
        is_escalation: bool = False,
    ) -> SkillBrief:
        """Suggest appropriate tone based on context."""
        if is_escalation:
            tone = "formal and firm but professional"
            recommendations = [
                "State the issue clearly with factual timeline",
                "Reference specific dates and missed deadlines",
                "Request specific action with a deadline",
            ]
        elif is_follow_up:
            tone = preferred_tone or "friendly but direct"
            recommendations = [
                "Reference the previous communication",
                "State what is still needed",
                "Offer help if they are blocked",
            ]
        else:
            tone = preferred_tone or "professional and helpful"
            recommendations = [
                "Start with context/greeting",
                "Be concise — aim for 3-5 sentences",
                "End with clear next step",
            ]

        return SkillBrief(
            skill_type=self.skill_type,
            observations=[f"Recommended tone: {tone}"],
            recommendations=recommendations,
            confidence=0.9,
        )
