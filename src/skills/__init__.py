"""
Skill Layer — domain-specific knowledge providers.
Skills are read-only and deterministic where possible.
They do NOT call LLMs independently.
"""

from src.skills.fix_onboarding import FIXOnboardingSkill
from src.skills.communication import CommunicationSkill

__all__ = ["FIXOnboardingSkill", "CommunicationSkill"]
