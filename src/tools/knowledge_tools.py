"""On-demand knowledge tool contracts (reference docs beyond catalog listing)."""

from src.tools.contracts import ToolContract

READ_REFERENCE_DOC = ToolContract(
    name="read_reference_doc",
    description=(
        "Read text from a standing reference document (FIX specs, conformance plan metadata). "
        "Call list_reference_docs first to see doc_key values. "
        "PDF/DOCX text is extracted; spreadsheets return catalog description only."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "doc_key": {
                "type": "string",
                "description": "Document key from list_reference_docs (e.g. fix_spec_connectivity_acceptance)",
            },
            "max_chars": {
                "type": "integer",
                "description": "Max characters to return (default 12000)",
            },
        },
        "required": ["doc_key"],
    },
    output_schema={"type": "object"},
    allowed_task_types=["*"],
    allowed_contexts=["*"],
    side_effect_type="read",
    query_safe=True,
    timeout_seconds=20,
)

KNOWLEDGE_TOOLS = [READ_REFERENCE_DOC]