"""
Telegram Interaction Card Standard (Blueprint Section 24).
Defines card profiles A-N and their button layouts.
"""

from dataclasses import dataclass, field

from src.telegram.i18n import t, button_display


@dataclass
class CardButton:
    """A button in a Telegram inline keyboard."""
    label: str
    callback_data: str


@dataclass
class InteractionCard:
    """A Telegram interaction card with structured content."""
    profile: str  # A-N
    title: str
    body: str
    project_id: str | None = None
    buttons: list[list[CardButton]] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


# Card Profile Definitions
_PROFILE_EMOJI: dict[str, str] = {
    "A": "🔍",   # Review Card
    "B": "📬",   # Triage Card
    "C": "✉️",   # Operational Draft Card
    "D": "📝",   # Draft Review Card
    "E": "👤",   # Identity Check Card
    "F": "🆕",   # New Project Intake Card
    "G": "⏰",   # Follow-up Nudge Card
    "H": "❓",   # Ambiguous Routing Card
    "I": "🔄",   # Deferred Review Card
    "J": "✅",   # Closing Checklist Card
    "K": "🔎",   # Query Clarification Card
    "L": "💬",   # Inbound Reply Card
    "M": "🚨",   # SLA Alert Card
    "N": "⚠️",   # LLM Error Card
    "O": "📋",   # Project Advisory Card
    "P": "🔧",   # Support Advisory Card
    "Q": "🤝",   # Commitment Confirmation Card
    "TK": "📎",  # Ticket thread follow-up (no assign — view only)
}

# Short profile label (shown in header line for context)
def _profile_label(profile: str) -> str:
    return t(f"cards.label.{profile}")


# Action hint shown at bottom of each card profile
def _profile_action_hint(profile: str) -> str:
    key = f"cards.hint.{profile}"
    val = t(key)
    return val if val != key else ""

CARD_PROFILES = {
    "A": {
        "name": "Review Card",
        "description": "ProposedUpdate review — operator approves/rejects truth changes",
        "buttons": [["APPROVE", "REJECT", "MODIFY"]],
    },
    "B": {
        "name": "Triage Card",
        "description": "Unassigned email — operator assigns to project or marks operational",
        "buttons": [["ASSIGN", "DETAILS"]],
    },
    "C": {
        "name": "Operational Draft Card",
        "description": "Draft for non-project operational email — summary view",
        "buttons": [["DETAILS", "DISCARD"]],
    },
    "D": {
        "name": "Draft Review Card",
        "description": "Project-related draft email — summary view",
        "buttons": [["DETAILS", "DISCARD"]],
    },
    "E": {
        "name": "Identity Check Card",
        "description": "New contact needs operator confirmation",
        "buttons": [["CONFIRM", "OVERRIDE", "FLAG"]],
    },
    "F": {
        "name": "New Project Intake Card",
        "description": "New project intake confirmation — operator confirms, edits details, or cancels",
        "buttons": [["CONFIRM", "EDIT", "CANCEL"]],
    },
    "G": {
        "name": "Follow-up Nudge Card",
        "description": "Internal follow-up reminder",
        "buttons": [["DONE", "CHANGE OWNER", "SNOOZE", "CANCEL"], ["DRAFT EMAIL"]],
    },
    "H": {
        "name": "Ambiguous Routing Card",
        "description": "Email matches multiple projects",
        "buttons": [],  # Dynamic — project buttons generated at runtime
    },
    "I": {
        "name": "Deferred Review Card",
        "description": "Previously deferred item eligible for re-evaluation",
        "buttons": [["RE-EVALUATE", "DISMISS", "SNOOZE 7DAYS"]],
    },
    "J": {
        "name": "Closing Checklist Card",
        "description": "Project closing checklist — confirm all items complete or postpone",
        "buttons": [["COMPLETE CLOSING", "POSTPONE"]],
    },
    "K": {
        "name": "Query Clarification Card",
        "description": "Aggregate query needs operator confirmation or filter modification before running",
        "buttons": [["CONFIRM & RUN", "MODIFY FILTERS", "CANCEL"]],
    },
    "L": {
        "name": "Inbound Reply Card",
        "description": "Client replied to a sent email",
        "buttons": [["CONFIRM UPDATE", "EDIT", "DISMISS"], ["📎 ATTACH TO TICKET"]],
    },
    "M": {
        "name": "SLA Alert Card",
        "description": "SLA or deadline alert — operator snoozes, marks done, or escalates",
        "buttons": [["SNOOZE 1DAY", "ACK", "ESCALATE"], ["DRAFT STATUS EMAIL"]],
    },
    "N": {
        "name": "LLM Error Card",
        "description": "LLM call failure alert — operator retries or dismisses",
        "buttons": [["RETRY", "DISMISS"]],
    },
    "O": {
        "name": "Project Advisory Card",
        "description": "Context-aware project advisory — operator drafts, handles, or asks more",
        "buttons": [["DRAFT EMAIL", "I HANDLE", "ASK MORE"]],
    },
    "P": {
        "name": "Support Advisory Card",
        "description": "Support ticket advisory — operator drafts reply, escalates, reclassifies, or handles",
        "buttons": [["DRAFT REPLY", "ESCALATE"], ["RECLASSIFY", "I HANDLE"]],
    },
    "Q": {
        "name": "Commitment Confirmation Card",
        "description": "Detected client commitment — operator confirms to create follow-up or dismisses",
        "buttons": [["CONFIRM", "DISMISS"]],
    },
    "CP": {
        "name": "Draft Preview Card",
        "description": "Full draft preview before send — operator confirms or discards",
        "buttons": [["CONFIRM SEND", "DISCARD"]],
    },
    "CU": {
        "name": "Staged Update Card",
        "description": "Chat-staged project field update — operator confirms or cancels",
        "buttons": [["CONFIRM", "CANCEL"]],
    },
    "TK": {
        "name": "Ticket Thread Update Card",
        "description": "Follow-up email linked to an open support ticket — view summary only",
        "buttons": [["DETAILS"]],
    },
}


def build_card(
    profile: str,
    title: str,
    body: str,
    project_id: str | None = None,
    extra_buttons: list[list[str | tuple[str, str]]] | None = None,
    action_revision: int | None = None,
) -> InteractionCard:
    """
    Build a Telegram InteractionCard from a profile template.

    extra_buttons: list of rows, where each row is a list of:
      - str label  → callback auto-generated as {profile}:{LABEL_UPPER}:{project_id}
      - (label, callback_data) tuple → callback_data used as-is (project_id NOT appended)
    """
    if profile == "G" and project_id and action_revision is None:
        raise ValueError("Profile G cards require action_revision")
    callback_entity = (
        f"{project_id}@{action_revision}"
        if profile == "G" and project_id is not None
        else project_id
    )
    profile_def = CARD_PROFILES.get(profile, {})
    button_layout = extra_buttons if extra_buttons is not None else profile_def.get("buttons", [])

    buttons = []
    for row in button_layout:
        btn_row = []
        for btn in row:
            if isinstance(btn, tuple):
                label, callback = button_display(btn[0]), btn[1]
            else:
                label = button_display(btn)
                callback = f"{profile}:{btn.upper().replace(' ', '_')}"
                if callback_entity:
                    callback += f":{callback_entity}"
            from src.telegram.operator_messages import button_label
            action = callback.split(":", 2)[1] if ":" in callback else ""
            label = button_label(profile, action, label)
            btn_row.append(CardButton(label=label, callback_data=callback))
        buttons.append(btn_row)

    return InteractionCard(
        profile=profile,
        title=title,
        body=body,
        project_id=project_id,
        buttons=buttons,
    )


def _card_dashboard_path(card: "InteractionCard") -> str:
    """Return the dashboard path for a card's deep link."""
    if card.profile == "A":
        return f"/reviews?q={card.project_id}"
    if card.profile in ("C", "D"):
        return f"/drafts?q={card.project_id}"
    ticket_id = card.metadata.get("ticket_id")
    if ticket_id:
        return f"/support/{ticket_id}"
    if card.profile == "P":
        return f"/support/{card.project_id}" if card.project_id else "/support"
    if card.profile == "J":
        return f"/projects/{card.project_id}/closing"
    return f"/projects/{card.project_id}"


def format_proposal_diff(
    current: dict, proposed: dict,
) -> str:
    """Format proposed field changes as a readable diff block for card body."""
    if not proposed:
        return ""
    lines = [t("cards.proposed_update")]
    for field, new_val in proposed.items():
        old_val = current.get(field) or "-"
        label = field.replace("_", " ").title()
        lines.append(f"  {label}: {old_val} → {new_val}")
    return "\n".join(lines)


def format_card_text(card: InteractionCard) -> str:
    """Format a card for Telegram message display (Markdown).

    Universal card template:
      {emoji} {TITLE}  ·  {PROJECT_ID}
      ━━━━━━━━━━━━━━━━━━━━━━━
      {body}

      {action_hint}
      {dashboard_link}
    """
    emoji = _PROFILE_EMOJI.get(card.profile, "")
    prefix = f"{emoji} " if emoji else ""
    # Header line: emoji + title + project ID
    header = f"**{prefix}{card.title}**"
    if card.project_id:
        header += f"  ·  `{card.project_id}`"
    lines = [header]
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━")

    # Body
    if card.body:
        lines.append(card.body)

    # Display only operations the actual keyboard offers; no profile-wide promises.
    labels = [button.label for row in card.buttons for button in row]
    if labels:
        from src.telegram.operator_messages import en
        lines.extend(["", ("Actions: " if en() else "可用操作：") + " · ".join(labels)])
    return "\n".join(lines)
