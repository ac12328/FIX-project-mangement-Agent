# AgentFIXBPRO — Agent-Driven AI Project Management

An agent-driven AI project management platform with FIX connectivity onboarding as its reference implementation.

AgentFIXBPRO coordinates multi-step work, not just conversations. Its agents combine project context, evidence and role-bound tools to investigate project status, delegate specialist work, prepare communication and carry out authorized workflow actions. Telegram and a web dashboard connect these workflows with operator review and management controls.

The platform brings project communication, milestones, responsibilities, next actions and follow-up work into a shared context. Its agent runtime is the reusable execution layer; FIX onboarding supplies the current reference workflow and domain rules.

## What the agents can do

The retained source architecture includes the following registered workflows and execution paths. These describe application capabilities, not a claim that this public preparation edition has completed runtime validation.

| Capability | Agent work |
|---|---|
| Multi-step investigation | Query project state, retrieve email evidence and Wiki knowledge, then choose follow-on tool calls based on the returned results. |
| Specialist orchestration | Delegate work to role- and skill-scoped specialists for research, project writes, email analysis, drafting, follow-ups and support. |
| Project operations | Stage project creation and field updates, capture notes and evidence, and process changes through the applicable authorization, confirmation and review paths. |
| Communication workflows | Analyze inbound email and create, retrieve, edit or discard drafts using project context and available source material. Creating a draft does not mean sending an email. |
| Portfolio and follow-up management | Inspect multiple projects, identify work needing attention, and create or complete follow-ups and internal actions. |
| Support work | Create or update support tickets, retrieve supporting evidence and record resolutions. |
| Personal work management | Manage tasks, commitments, waiting-for items and reminders; use registered meeting and calendar tools within their product-surface controls. |
| Context and operator memory | Combine project facts, working/conversation memory, relationship preferences and Wiki knowledge; persist operator preferences through a dedicated memory skill. |
| Progress and accountability | Expose execution progress and record tool/delegation outcomes and traces alongside evidence and operation audit records. |

Agentic execution does not mean unrestricted autonomy. In orchestrator mode, the host agent reads and delegates; it does not receive direct write primitives. Specialists use skill-specific tool allowlists. Project writes, knowledge changes and outbound delivery retain their respective evidence, authorization, review and confirmation controls. A proposal, a staged update, a committed change and a delivered message are different outcomes.

## Example: from a request to coordinated work

**Newly authored synthetic illustration — not a private case, an operational record or a verified end-to-end run.**

> Review the projects that need attention, identify the latest supporting email evidence, arrange internal follow-ups and prepare reply drafts where appropriate.

The registered workflows can be composed into a sequence such as:

1. Query the project portfolio and inspect status, ownership, next actions and open work.
2. Retrieve relevant email evidence and available Wiki guidance; distinguish missing information from established facts.
3. Use the returned information to decide which specialist workflow is needed next.
4. Delegate authorized follow-up work and draft preparation to the corresponding specialists.
5. Return the actual tool outcomes, distinguishing completed work from proposals, pending review, failures and unknown outcomes.

This illustrates the agent's ability to move from investigation to coordinated action, rather than merely producing a suggested checklist. It does not imply that every request follows this exact sequence or that reply drafts are automatically sent. No real client identities, events, email contents or private Wiki sources are reconstructed in this example.

## How the AI works — Unified Agent Loop

The unified loop is the shared model-and-tool execution engine behind these workflows. Instead of making one model call and treating its text as an operation result, the runtime can make bounded rounds of model decisions, tool calls and tool-result feedback.

```text
Request + scoped context + role/skill tools
                    |
                    v
            Model selects next step
                    |
                    v
        Allowed tool call or delegation
                    |
                    v
              Actual tool result
                    |
                    +----> Next model round, within the configured budget
                    |
                    v
       Final response or bounded error/exhaustion outcome
                    |
                    v
       Surface-appropriate result handling and tracing
```

The source separates the responsibilities:

| Source | Responsibility |
|---|---|
| [Unified entry](src/agent/unified_entry.py) | Represent requests with `LoopRequest` and invoke the shared surface loop. |
| [Loop preparation](src/agent/prepare_loop.py) | Select tools, inject guards, filter unavailable operations and set the round budget for paths using this preparation layer. |
| [Core loop](src/agent/core_loop.py) | Run transport-independent model/tool rounds and collect their results. |
| [Harness](src/agent/harness.py) | Handle tool execution, required-tool nudges, finalization, errors and budget exhaustion. |
| [Shared post-processing](src/agent/post_loop.py) | Apply grounded response cleanup and shared post-loop handling where enabled. |
| [Context engine](src/context/engine.py) | Assemble typed project, memory, evidence and knowledge context. |
| [Skill registry](src/agent/skills/registry.py) and [specialist runner](src/agent/specialists/runner.py) | Define workflow-specific tool boundaries and execute delegated specialist work. |

Telegram and Web Workspace agent paths use the shared entry, while specialist runners reuse the core loop. Their preparation, permissions and persistence are not identical; for example, the Web Workspace query loop uses read-only execution and caller-owned result handling. This is not a claim that every API endpoint, email path or scheduled job uses the same entry or has the same authority.

Context and memory support continuity; they are not model retraining and do not turn unsupported statements into facts. Wiki prose supplies knowledge context, while executable domain contracts and review services control accepted project-state changes. The bundled public Wiki remains draft/low-confidence content, not approved operational knowledge.

## Why FIX onboarding?

FIX connectivity onboarding demonstrates a structured, evidence-driven workflow involving multiple teams, staged testing and operational handoffs. It provides a concrete setting in which the agents can connect communication, evidence, project state and follow-up actions.

The design aims to make its project-management capabilities reusable across domains. The current implementation still contains FIX-specific milestones and policy rules; adapting it to another workflow requires further engineering and validation. The shared agent engine and the FIX-specific domain rules should not be confused with an already configurable, domain-neutral product.

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
