# Skill: email_to_truth

Use the focused deterministic reconciliation workflow to align project truth with historical email evidence.

## Preferred protocol

1. Identify the project ID from the operator message or trusted active project scope.
2. If the operator explicitly says **update**, **apply**, **confirm**, or equivalent, call:
   - `project_reconcile(project_id=..., mode="commit")`
3. If the operator asks to **inspect**, **check**, **estimate**, **propose**, or **preview**, call:
   - `project_reconcile(project_id=..., mode="preview")`
4. Use `create_if_missing=true` only when the operator explicitly asks to open/start a missing project from the email evidence.
5. Return the operation receipt, confirmed DB fields, and whether project truth changed.

`project_reconcile` freezes historical email evidence, extracts deterministic facts, replays the versioned milestone contract, checkpoints the proposal, and either previews or commits atomically. Do not replace it with the older search → stage → confirm chain when this tool is available.

## Safety

- Email analysis is not the final write decision and is not write authorization.
- Never claim a commit without `committed=true` and a confirmed operation receipt.
- Never trust model-supplied authorization flags.
- Never invent milestone evidence, session details, or citations.
- If the result is preview-only or failed, state clearly that project truth was not changed.
