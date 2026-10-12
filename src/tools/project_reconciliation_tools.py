"""Tool contract for deterministic email-to-project reconciliation."""

from src.tools.contracts import ToolContract


PROJECT_RECONCILE_TOOL = ToolContract(
    name="project_reconcile",
    description=(
        "Deterministically reconcile one project's current truth from frozen historical "
        "email evidence. Use preview to inspect/propose and commit only after an explicit "
        "operator instruction to update."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "project_id": {
                "type": "string",
                "description": "Canonical project identifier from the verified project record.",
            },
            "mode": {
                "type": "string",
                "enum": ["preview", "commit"],
                "default": "preview",
            },
            "create_if_missing": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Create a missing project only when the operator explicitly asked to "
                    "open/start it from the email evidence."
                ),
            },
            "resume_operation_id": {
                "type": "string",
                "description": "Optional owned reconciliation operation to resume.",
            },
        },
        "required": ["project_id"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "status": {"type": "string"},
            "outcome": {"type": "string"},
            "committed": {"type": "boolean"},
            "project_id": {"type": "string"},
            "operation_id": {"type": "string"},
            "project_created": {"type": "boolean"},
            "applied_fields": {"type": "object"},
            "proposed_fields": {"type": "object"},
            "current_project": {"type": ["object", "null"]},
            "bundle_hash": {"type": ["string", "null"]},
            "evidence_refs": {"type": "array", "items": {"type": "string"}},
            "proposed_update_id": {"type": ["string", "null"]},
            "error_code": {"type": ["string", "null"]},
            "error": {"type": ["string", "null"]},
            "text": {"type": "string"},
        },
        "required": ["ok", "status", "outcome", "project_id", "operation_id"],
    },
    allowed_task_types=["*"],
    allowed_contexts=["*"],
    side_effect_type="write",
    timeout_seconds=20,
    retry_policy="once",
    idempotency_mode="trusted_key",
    audit_required=True,
)


PROJECT_RECONCILIATION_TOOLS = [PROJECT_RECONCILE_TOOL]
