"""
Milestone state machine.
Defines valid transitions, M7 FAIL→M5 revert logic.
"""

from src.models.enums import Milestone

# Ordered sequence of milestones
MILESTONE_SEQUENCE: list[Milestone] = list(Milestone)

# Valid transitions: forward +1, plus M7 → M5 revert
ALLOWED_TRANSITIONS: dict[Milestone, list[Milestone]] = {}
for i, ms in enumerate(MILESTONE_SEQUENCE):
    targets = []
    if i + 1 < len(MILESTONE_SEQUENCE):
        targets.append(MILESTONE_SEQUENCE[i + 1])
    ALLOWED_TRANSITIONS[ms] = targets


def is_valid_transition(from_milestone: Milestone | None, to_milestone: Milestone) -> bool:
    """
    Check if a milestone transition is valid.

    Rules:
    - None → M1 is always valid (initial assignment)
    - Forward +1 step is valid
    - M7 → M5 revert is valid (FAIL case)
    - Everything else is invalid
    """
    if from_milestone is None:
        return to_milestone == Milestone.M1_NETWORK_SETUP
    return to_milestone in ALLOWED_TRANSITIONS.get(from_milestone, [])


def is_revert_transition(from_milestone: Milestone, to_milestone: Milestone) -> bool:
    """Backward milestone transitions are not allowed."""
    return False


def get_next_milestone(current: Milestone) -> Milestone | None:
    """Get the next milestone in sequence, or None if at M8."""
    idx = current.index
    if idx + 1 < len(MILESTONE_SEQUENCE):
        return MILESTONE_SEQUENCE[idx + 1]
    return None
