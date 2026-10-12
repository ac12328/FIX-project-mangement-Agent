# Public Preparation Guidance

This file is the single source of truth for this independent preparation edition. Read README.md and docs/PUBLIC_DEMO_POLICY.md before changing files.

## Language
- Use Traditional Chinese for discussion and reporting.
- Use English for code, comments, identifiers, documentation and commit messages.

## Scope and safety
- Work only inside this public preparation copy. Never open production configuration, mail, attachments or databases, modify another workspace, inherit its credentials, or operate its services.
- Standard runtime launchers and service controls are intentionally blocked. Do not bypass them or initialize/push Git without explicit approval.
- Direct imports and alternate launch commands are not a sandbox. Future runtime tests require an isolated environment, fresh database and outbound isolation.
- Private reports, reverse mappings, backups and excluded originals belong outside this tree. Historical cases must be withheld, not relabeled/date-shifted as synthetic cases.
- Do not invent Wiki sources, operational events, approvals, PDFs, FIX traces, endpoints or market rules. Demo Wiki prose is draft/low confidence, not an approved source.
- No production-data migration, full-test coverage, license or publication clearance is implied.

## Architecture
The source retains a shared Telegram/FastAPI runtime, SQLite repositories, multi-round model/tool loop, email processing, governed learning interfaces and Next.js dashboard. Runtime functionality is not certified in this edition.
- src/agent/unified_entry.py, prepare_loop.py, core_loop.py, harness.py and post_loop.py own the shared agent path.
- Orchestrator hosts remain read/delegate-only. Keep tool contracts, exposure and handlers aligned.
- src/context/engine.py assembles typed context. Preserve project scope and evidence provenance.
- Use scoped_read()/scoped_write(); project_repo.update_project_fields() is the low-level project writer and safe_write_project() validates writes.
- Python enums, SQLite checks, the milestone JSON contract and TypeScript/API unions must agree.
- Preserve existing event publication, evidence, review, recipient, confirmation, idempotency and uncertain-delivery guards.
- Keep modules focused; avoid adding new large orchestration modules.

## Domain invariants
- Eight milestones run from M1_NetworkSetup through M8_GoLive, with coordinated demo stage identifiers.
- Milestone, project status, ball-with and next-action are distinct dimensions.
- New projects start at M1; evidenced progress cannot invent intermediate history or regress stages.
- The executable owner is wiki/playbook/fix-connectivity/milestone-contracts.json. Draft Wiki and prompts cannot override it.
- Inferred milestone updates remain operator-review-only. The narrowly source-authorized PCT completion path is a specific exception, not permission to bypass other review gates.
- Demo Provisioning Support and Demo Connectivity Acceptance Support retain distinct capabilities as documented in docs/PUBLIC_DEMO_POLICY.md. Do not merge them.
- PCT completion couples M8 and Completed; residual work is separately linked support work, not reopening onboarding.
- Commit notification/delivery intent in the same transaction as the state change. Dispatch only committed outbox rows; uncertain Telegram outcomes require manual reconciliation and must not be automatically replayed.
- Evidence types remain email, commitment, fact, attachment and manual_note. Follow-up statuses remain open, completed, overdue and cancelled.
- Store timestamps as UTC ISO 8601 using SQLite datetime('now').

## Wiki
Read wiki/WIKI_GUIDE.md. Only newly authored generic draft pages are bundled; internal creation-policy prose, institutional market corpus, reference attachments and learned snapshots are absent. Migration formatting must not promote content to approved knowledge. Use guarded knowledge tools, not unrestricted writes.

## Dashboard
Read dashboard/AGENTS.md and dashboard/CLAUDE.md. Use semantic theme tokens, shared API wrappers and existing UI components. No framework/API redesign is part of de-identification. The generated API union is coordinated by identifier replacement only, not regenerated or type-checked here.

## Checks
Use the Windows py launcher. The independent preparation suite is:

    py -B -m unittest discover -s tests/public_demo -v

It uses extracted source and in-memory SQLite, not app imports or production files. Inherited product and dashboard tests are withheld for independent review. Do not claim that syntax checks or these narrow tests prove complete runtime functionality or security.

## Open decisions
Read the policy limits before changing the M7 short-form fallback, M5 evidence wording, advisory flag conflicts or other policy behavior. De-identification alone does not resolve these issues. Ownership/rights/license and publication remain owner decisions.
