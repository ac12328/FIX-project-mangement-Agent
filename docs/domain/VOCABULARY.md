# Domain Vocabulary

Canonical status, ball, and work-item vocabulary for AgentFIXBPRO.
Source of truth for enum values: `src/models/enums.py`.
Pure cross-entity maps: `src/models/mapping.py`.

This document is the operator/agent-facing table. It does **not** change storage
or production call sites by itself.

---

## ProjectStatus

Stored on `projects.status`. **Title Case** legacy storage form (decision D6:
do **not** migrate to lowercase).

| Enum member | Stored value | Terminal? | WorkItemStatus map |
|-------------|--------------|-----------|--------------------|
| `ACTIVE` | `Active` | No | `open` |
| `BLOCKED` | `Blocked` | No | `open` |
| `WAITING` | `Waiting` | No | `open` |
| `COMPLETED` | `Completed` | Yes | `closed` |
| `ARCHIVED` | `Archived` | Yes | `closed` |
| `CANCELLED` | `Cancelled` | Yes | `closed` |

Helpers: `is_project_terminal()`, `project_status_to_work_item_status()`.

**Decision:** Do **not** migrate Project Title Case storage to lowercase. New
enums stay lowercase/snake unless matching existing project fields.

---

## Milestone

Canonical FIX onboarding stage stored on `projects.current_milestone`.
The operational stage-gate source of truth is
`wiki/playbook/fix-connectivity/milestone-contracts.json`.

| Enum member | Stored value | Stage entered |
|-------------|--------------|---------------|
| `M1_NETWORK_SETUP` | `M1_NetworkSetup` | Initial UAT network setup |
| `M2_UAT_SESSION_CONFIGURATION` | `M2_UATSessionConfiguration` | UAT FIX session provisioning |
| `M3_UAT_ENVIRONMENT_ACCESS` | `M3_UATEnvironmentAccess` | Validated UAT access and basic flow |
| `M4_APPLICATION_TESTING` | `M4_ApplicationTesting` | Demo Gateway A/counterparty configuration |
| `M5_CONFORMANCE_TEST_CASES` | `M5_ConformanceTestCases` | Formal conformance execution |
| `M6_TEST_REPORT_AND_PRODUCTION_PREPARATION` | `M6_TestReportAndProductionPreparation` | Report acceptance and Production Preparation/configuration |
| `M7_LOG_REVIEW_AND_PRODUCTION_SESSION_DELIVERY` | `M7_LogReviewAndProductionSessionDelivery` | Final log review, production session, and applicable PCT |
| `M8_GO_LIVE` | `M8_GoLive` | Production launch/operation |

`current_milestone` means the **highest validated onboarding stage entered**.
Advancing from Mn to Mn+1 means Mn exit criteria are satisfied and the project
has entered Mn+1.

Locked rules:

- Milestone, `ProjectStatus`, `BallWith`, and `next_action` are separate dimensions.
- M8 does not automatically imply `Completed`.
- Lower-layer blockers do not automatically regress the milestone.
- Normal movement is adjacent forward; jumps are protected reviewed exceptions.
- M7 to M5 is the only supported fail-case revert.
- Typical stage ball-with is advisory only and cannot support a canonical write.
- All inferred milestone changes require operator review.

---

## BallWith

Canonical “who has the ball” for **projects** (stored on `projects.ball_with`).

| Enum member | Stored value | Meaning |
|-------------|--------------|---------|
| `INTERNAL` | `Internal` | Ball is with us (internal team) |
| `EXTERNAL` | `External` | Ball is with client / external party |
| `SHARED` | `Shared` | Parallel ownership / joint action |
| `UNKNOWN` | `Unknown` | Unclear or not applicable |

---

## TicketStatus

Stored on `support_tickets.status`. Lowercase snake values.

| Enum member | Stored value | Terminal? | WorkItemStatus map |
|-------------|--------------|-----------|--------------------|
| `OPEN` | `open` | No | `open` |
| `INVESTIGATING` | `investigating` | No | `open` |
| `WAITING_EXTERNAL` | `waiting_external` | No | `open` |
| `WAITING_INTERNAL` | `waiting_internal` | No | `open` |
| `RESOLVED` | `resolved` | Yes | `resolved` |
| `CLOSED` | `closed` | Yes | `closed` |

Helpers: `is_ticket_terminal()`, `ticket_status_to_work_item_status()`.

Note: ticket `resolved` maps to WorkItem `resolved` (not `closed`), so the
envelope can distinguish “done pending archive” vs fully closed.

---

## WorkItemStatus

Coarse envelope on `work_items.status`. Detail always lives on the entity
(`ProjectStatus` / `TicketStatus`).

| Value | Meaning |
|-------|---------|
| `open` | Entity is non-terminal |
| `resolved` | Ticket resolved (project terminals do not use this) |
| `closed` | Project terminal, or ticket closed |

Mapping rules (see `src/models/mapping.py`):

- Project terminal (`Completed` / `Archived` / `Cancelled`) → `closed`
- Ticket `resolved` → `resolved`
- Ticket `closed` → `closed`
- All other project/ticket statuses → `open`

---

## Ticket ball (legacy labels)

Tickets do **not** currently store `BallWith`. Derived ball uses legacy string
labels (`client` / `us` / `resolved` / `unknown`).

| Legacy label | Canonical `BallWith` |
|--------------|----------------------|
| `us` | `Internal` |
| `client` | `External` |
| `resolved` | `Unknown` |
| `unknown` | `Unknown` |
| (any other) | `Unknown` |

Reverse compat map (dashboard one-release alias if needed):

| `BallWith` | Legacy label |
|------------|--------------|
| `Internal` | `us` |
| `External` | `client` |
| `Shared` | `unknown` |
| `Unknown` | `unknown` |

Helpers: `ticket_legacy_ball_to_ball_with()`, `ball_with_to_ticket_legacy()`.

**Phase 2:** Unify product language on `BallWith` (derived for tickets; stored
for projects). API may add `ball_with` (canonical) and keep a short-lived
legacy alias if the dashboard needs it.

---

## TaskType (BusinessTask) vs LlmRoute

Two layers — do **not** store pure routing keys as business `task_type`.

| Layer | Type | Module | Role |
|-------|------|--------|------|
| Business | `TaskType` (alias concept: BusinessTask) | `src/models/enums.py` | Write / draft / audit / specialist semantics; stored on proposed updates / drafts where needed |
| Routing | `LlmRoute` | `src/models/tasks.py` | Keys into `TASK_MODEL_MAP` only; never stored as business `task_type` |

Maps and helpers live in `src/models/tasks.py` (re-exported from `src/models/mapping.py`):

- `SPECIALIST_TO_BUSINESS_TASK` — `SpecialistType` value → `TaskType`
- `BUSINESS_TASK_TO_LLM_ROUTE` — `TaskType` → preferred `LlmRoute`
- `resolve_llm_route(task)` — normalize `str | TaskType | LlmRoute` → `TASK_MODEL_MAP` key
- `specialist_to_business_task(specialist)` — `SpecialistType` / string → `TaskType`

### TaskType (business)

| Enum member | Stored value | Preferred LlmRoute |
|-------------|--------------|--------------------|
| `TRUTH_WRITE_REASONING` | `truth_write_reasoning` | `truth_write_reasoning` |
| `REPLY_TO_CLIENT` | `reply_to_client` | `draft` |
| `DRAFT_FOLLOW_UP` | `draft_follow_up` | `draft` |
| `ANSWER_STATUS` | `answer_status` | `status` |
| `INTAKE_TRIAGE` | `intake_triage` | `triage` |
| `INTERNAL_FOLLOW_UP` | `internal_follow_up` | `operational` |
| `MORNING_BRIEF` | `morning_brief` | `morning_brief` |
| `AGGREGATE_QUERY` | `aggregate_query` | `reasoning` |
| `SUPPORT_REPLY` | `support_reply` | `draft` |
| `RESEARCH` | `research` | `analysis` |
| `PORTFOLIO_SCAN` | `portfolio_scan` | `reasoning` |

### LlmRoute (routing-only examples)

Pure routing keys that are **not** business `TaskType` values include:
`wiki_curator`, `flush`, `image_description`, `knowledge_mining`, `insight_mining`,
`inbound_reply_analysis`, `feedback_rule_extraction`, `lead_reasoning`, `email_draft`,
`learning`, `analysis`, `operational`, etc.

`get_model_for_task()` accepts either layer (and unknown strings for backward compat)
via `resolve_llm_route()`.

### SpecialistType → TaskType

| SpecialistType | TaskType |
|----------------|----------|
| `status` | `answer_status` |
| `research` | `research` |
| `write` | `truth_write_reasoning` |
| `draft` | `reply_to_client` |
| `portfolio` | `portfolio_scan` |
| `support` | `support_reply` |
| `email` | `reply_to_client` |

---

## EvidenceType (project)

Stored on project `evidence_store.evidence_type`. **Not** the same set as ticket evidence.

| Enum member | Stored value |
|-------------|--------------|
| `EMAIL` | `email` |
| `COMMITMENT` | `commitment` |
| `FACT` | `fact` |
| `ATTACHMENT` | `attachment` |
| `MANUAL_NOTE` | `manual_note` |

---

## TicketEvidenceType

Stored on `ticket_evidence.evidence_type` (CHECK constraint). Domain-specific; do **not** force a single shared enum with project `EvidenceType`.

| Enum member | Stored value |
|-------------|--------------|
| `EMAIL` | `email` |
| `NOTE` | `note` |
| `LOG_SNIPPET` | `log_snippet` |
| `SCREENSHOT` | `screenshot` |
| `RESOLUTION` | `resolution` |

Model: `TicketEvidence.evidence_type: TicketEvidenceType \| str` with `use_enum_values=True`.

---

## InternalActionStatus

Stored on `internal_actions.status`. **Different vocabulary** from `FollowupStatus` (`completed` vs `done`).

| Enum member | Stored value | Meaning |
|-------------|--------------|---------|
| `OPEN` | `open` | Team task still open |
| `DONE` | `done` | Completed |
| `CANCELLED` | `cancelled` | Cancelled |

Do **not** merge with `internal_followups` (`FollowupStatus`: `open` / `completed` / `overdue` / `cancelled`).

---

## OwnerTeam

Stored on `internal_actions.owner_team` (CHECK). Single source: `OwnerTeam` enum; `OWNER_TEAMS` tuple is derived as `tuple(t.value for t in OwnerTeam)`.

| Enum member | Stored value |
|-------------|--------------|
| `IT_NETWORK` | `IT Network` |
| `COMPLIANCE` | `Compliance` |
| `PROVISIONING_SUPPORT` | `Demo Provisioning Support` |
| `CONNECTIVITY_ACCEPTANCE_SUPPORT` | `Demo Connectivity Acceptance Support` |
| `DEMO_IT_SECURITY` | `Demo IT Security` |
| `DEMO_TRADING_OPS` | `Demo Trading Ops` |
| `DEMO_POST_TRADE` | `Demo Post-Trade` |

---

## Portfolio health counters

| Field | Source table | Meaning |
|-------|--------------|---------|
| `overdue_internal_actions` | `internal_actions` | Open team actions past `deadline` |
| `overdue_internal_followups` | `internal_followups` | Open follow-ups past `due_date` |

These are **not** the same work type — keep counters and UI labels distinct.

---

## Coming later

| Concept | Phase | Notes |
|---------|-------|-------|
| Memory ownership table | Phase 5 | See `docs/domain/MEMORY_OWNERSHIP.md` (when added) |

---

## Timeline spines (intentional triple)

Three separate timeline tables are **intentional**, not accidental duplication.
No code merge is planned in the domain coherence program; a future unified
timeline API may project all three.

| Table | Purpose | Primary writers |
|-------|---------|-----------------|
| `project_events` | Email/activity spine | triage, email pipeline |
| `episodes` | Narrative PM timeline | agent / learning |
| `activity_log` | Immutable operator/agent actions | write layer, services |

---

## Design decisions (locked)

1. **Ball vocabulary** — Unify on existing `BallWith` for product language.
2. **Ticket ball storage** — Keep derived; no new `support_tickets.ball_with` column in v1.
3. **WorkItem status** — Coarse envelope `open` \| `resolved` \| `closed`; must stay in sync with entity terminal state.
4. **Project status casing** — **Do not migrate** Title Case (`Active`, …) to lowercase.
5. **Task taxonomy** — Keep name `TaskType` for business; introduce `LlmRoute` for routing only. No pure routing keys in `TaskType`.
6. **Timeline spines** — Keep `project_events`, `episodes`, and `activity_log` as three intentional spines (no merge in this program).
