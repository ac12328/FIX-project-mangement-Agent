# AgentFIXBPRO — AI-Assisted Project Management

An AI-assisted project management platform with FIX connectivity onboarding as its reference implementation.

AgentFIXBPRO brings project communication, evidence, milestones and follow-up work into a shared context. It helps teams understand project status, identify who owns the next action, and coordinate work through AI-assisted recommendations, review controls and management interfaces.

## Core capabilities

- Organize project communication and supporting evidence.
- Track milestones, status, responsibilities and next actions.
- Assist with project analysis and response drafting.
- Manage follow-up work, commitments and reminders.
- Provide Telegram and web dashboard interfaces.
- Preserve evidence provenance, review gates and delivery-confirmation controls.

## Why FIX onboarding?

FIX connectivity onboarding demonstrates a structured, evidence-driven workflow involving multiple teams, staged testing and operational handoffs.

The design aims to make its project-management capabilities reusable across domains. The current implementation still contains FIX-specific milestones and policy rules; adapting it to another workflow requires further engineering and validation.

## Public edition status

This repository contains a de-identified preparation snapshot. Private organizational data and historical cases are excluded. Runtime startup remains intentionally blocked, and this edition has not completed full application or deployment validation.

Prepared on 2026-10-12.

This independent source snapshot has no original Git history or shared worktree. The owner separately authorized publication of this incomplete preparation edition on 2026-10-12. This authorization does not establish third-party rights, institutional know-how clearance, a license, policy coherence or runtime readiness.

## What is included
- Generic Python application structure, SQLite definitions, API types and dashboard source.
- A coordinated eight-stage milestone vocabulary and distinct fictional support roles.
- Newly authored draft/low-confidence Wiki explanations and a blank email template.
- Independent, narrowly scoped offline de-identification checks.

Read docs/PUBLIC_DEMO_POLICY.md for role capabilities, knowledge provenance, withheld material and known policy conflicts.

## What is not included
No live settings, credentials, database, mail archive, attachments, logs, learned snapshot, original Git history or institutional Wiki corpus is supplied. The inherited Python/dashboard tests and historical replay/seed tooling are withheld intact outside this tree pending independent synthetic-fixture review; not every withheld file is asserted to contain personal data. They have not been renamed into fictional cases. No real-client events, FIX logs, certificates, CompIDs, market instructions or reference PDFs have been invented.

## Fictional boundaries
Demo Provisioning Support and Demo Connectivity Acceptance Support are separate roles. Reserved demo mailboxes/domains are placeholders, not real contacts or SMTP authentication. The vendor profile is newly configured, not a mapped real vendor roster. Reverse mappings and excluded originals are private and are not part of this folder.

Milestone storage identifiers and consumers have changed. Use a fresh demo database only; no production-data migration or backward compatibility is promised.

## Runtime safety
The service controls and standard launchers deliberately stop with a preparation notice. Bot/API startup is blocked, inbound integrations and registration are off in the example template, Windows credential hydration is disabled, and application-specific credential environment names use AGENTFIX_DEMO_. Demo defaults use loopback API port 8317 and dashboard port 3317.

These precautions are not a sandbox: direct module imports or alternate launch commands can still reach integration code. Do not reuse a production process environment, settings, database or running service. Any future runtime validation must use an isolated environment with outbound access disabled.

## Offline checks
Only standard-library, source-extraction tests under tests/public_demo/ are intended for this preparation edition:

    py -B -m unittest discover -s tests/public_demo -v

They check identifier/schema/contract consistency and narrow source-authorization behavior without loading application configuration or accessing a real database. They do not replace the withheld product suite, a dashboard type check/build, browser tests or end-to-end deployment testing.

## Before a runnable or certified release
1. Review the private de-identification report, mapping and file manifest outside this folder.
2. Resolve the documented contract/prose conflicts and approve missing operational sources independently.
3. Rebuild synthetic test fixtures and validate an isolated runtime and dashboard.
4. Confirm ownership, third-party rights, institutional know-how clearance and a license.
5. Review every future staged-file/history change and publish it only after explicit owner approval.

No license is selected. The earlier de-identification pass did not initialize or push Git; preparation-snapshot publication is a separate owner-authorized action. Outstanding release decisions above remain unresolved.
