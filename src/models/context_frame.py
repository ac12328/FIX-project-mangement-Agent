"""
ContextFrame — unified context output for all agent entry points (WS-2).

Sole production assembly path via ContextEngine. Replaces ContextPacket
(deprecated; see context_packet.py) and inline message assembly in chat.

Memory ownership across layers: docs/domain/MEMORY_OWNERSHIP.md.
L2/L3 structured models: src/models/context_layers.py.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from src.memory.exposure_repo import ExposureCandidate


class ContextFrame(BaseModel):
    """Structured context assembled by ContextEngine.

    Slots map to the conceptual layers:
      identity     — agent identity/role (system prompt core)
      project_truth — L1: project record, milestone, ball_with
      relationship  — L3: contact style, tone, greeting
      evidence      — L4: recent evidence, commitments
      knowledge     — wiki chunks, market rules, learned rules
      memory        — working memory, conversation summary
      conversation  — compressed chat history (message dicts)
      instructions  — task-specific prompt / planning hints
    """

    identity: str = ""
    project_truth: str = ""
    relationship: str = ""
    relationship_truth: str = ""
    evidence: str = ""
    knowledge: str = ""
    operational_memory: str = ""
    learned_memory: str = ""
    memory_exposure_candidates: list[ExposureCandidate] = Field(default_factory=list)
    memory: str = ""
    conversation: list[dict[str, Any]] = Field(default_factory=list)
    instructions: str = ""
    context_confidence: float = Field(ge=0.0, le=1.0, default=1.0)
    missing_flags: list[str] = Field(default_factory=list)

    def to_system_prompt(self) -> str:
        """Return the system-message content (identity + knowledge + evidence + relationship)."""
        msgs = self.to_messages()
        for m in msgs:
            if m.get("role") == "system":
                return m["content"]
        return ""

    def memory_exposure_scope(
        self, *, operator_id: str, surface: str,
        output_revision_id: str | None = None,
        task_type: str | None = None, project_id: str | None = None,
    ):
        """Return immutable exposure scope data for a later inference boundary."""
        from src.memory.exposure_context import make_exposure_scope

        return make_exposure_scope(
            operator_id=operator_id, surface=surface,
            output_revision_id=output_revision_id,
            task_type=task_type, project_id=project_id,
            candidates=self.memory_exposure_candidates,
        )

    def to_messages(self) -> list[dict[str, Any]]:
        """Convert the frame into an LLM-ready message list.

        Layout:
          1. system message — identity + instructions + fenced knowledge/rules
          2. memory/project context as user message (fenced)
          3. conversation history
        """
        # Build system content
        parts = [self.identity] if self.identity else []
        if self.instructions:
            parts.append(self.instructions)
        if self.context_confidence < 0.4:
            missing = ", ".join(sorted(set(self.missing_flags))) or "unspecified"
            parts.append(
                "<context-quality "
                f'confidence="{self.context_confidence:.3f}" '
                'minimum-for-sensitive-actions="0.400">\n'
                "Context is incomplete. Before any sensitive project-truth "
                "write, external action, or client draft, use available read "
                "tools to retrieve the current state. If the missing facts "
                "cannot be resolved, ask the operator for clarification. Do "
                "not infer or perform the sensitive action from incomplete "
                f"context. Missing context: {missing}.\n"
                "</context-quality>"
            )
        if self.knowledge:
            parts.append(f"<knowledge>\n{self.knowledge}\n</knowledge>")
        if self.evidence:
            parts.append(f"<evidence>\n{self.evidence}\n</evidence>")
        relationship = "\n".join(
            part for part in (self.relationship_truth, self.relationship) if part
        )
        if relationship:
            parts.append(f"<relationship>\n{relationship}\n</relationship>")

        content = "\n\n".join(parts) if parts else "You are a helpful assistant."
        messages: list[dict[str, Any]] = [{"role": "system", "content": content}]

        # Inject memory + project truth as fenced user context
        context_parts: list[str] = []
        if self.memory:
            context_parts.append(
                f'<memory-context note="Recalled context, NOT new user input">\n'
                f"{self.memory}\n</memory-context>"
            )
        if self.project_truth:
            context_parts.append(
                f"<project-context>\n{self.project_truth}\n</project-context>"
            )
        if context_parts:
            messages.append({"role": "user", "content": "\n\n".join(context_parts)})

        # Append conversation history
        messages.extend(self.conversation)

        return messages
