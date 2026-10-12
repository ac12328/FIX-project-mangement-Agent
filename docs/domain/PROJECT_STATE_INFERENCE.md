# Project-State Inference and Operations Runbook

## Authority and invariants

The single policy owner for milestone decisions is the versioned contract at
`wiki/playbook/fix-connectivity/milestone-contracts.json`, loaded and validated by
`src/milestone/contracts.py`, then applied by `src/project_state/`. Wiki prose, LLM
field suggestions, and legacy regex modules are retrieval or shadow hints only.
They cannot establish milestone completion or bypass evidence and authorization checks.

Project state has four dimensions: milestone, project status, ball-with, and next
action. The confirmed FIX onboarding policy couples authorized PCT completion to
M8 and Completed together; residual issues continue in linked support tickets
without regressing or reopening onboarding. Ball-with follows actual next actions,
and deadlines require explicit commitments.

**Implementation/rollout boundary:** legacy review and Dashboard-only closing
checks still exist while the verified evidence-driven path is being integrated.
Do not disable them merely to make the target policy appear implemented. Inferred
milestone changes remain subject to the currently enabled review/write gates until
authorized source binding, atomic M8/Completed persistence, and failure recovery
are verified. Do not invent historical events to fill gaps.

## End-to-end data flow

1. Email, attachment, operator input, or another source is extracted into typed
   facts with source references, bounded quotes, modality, event time, and
   extraction confidence.
2. `src/email/project_state_assessment.py` bounds email and attachment citations
   to source content and assembles `ProjectStateInput` from current project truth.
3. `src/project_state/evaluator.py` evaluates milestone criteria, status, ball-with,
   and next action independently against the loaded policy. Missing identity,
   unsupported policy versions, malformed facts, unbounded citations, and contract
   load errors fail closed: no milestone, status, ownership, or next-action candidate is forwarded from an invalid assessment.
4. Proposal metadata carries the assessment JSON, policy version, source type,
   source reference, criterion results, citations, contradictions, and confidence.
5. `src/services/review_service.py` is the shared operator-review gate for API and
   Telegram surfaces. Approval revalidates metadata and uses compare-and-swap
   checks so a stale review cannot overwrite newer project truth.
6. Accepted changes pass through `safe_write_project()` and the repository write
   path; no evaluator, prompt, wiki helper, or audit script writes projects directly.

## Evidence and fail-closed behavior

Evidence levels and accepted modalities are defined in the contract. Citations must
identify the source and include a quote bounded by the email or attachment content.
Future plans, requests, generic keywords, unsupported sources, missing required
criteria, contradictions, or ambiguous project identity cannot prove an exit gate.
The evaluator records met, missing, contradicted, and not-applicable results and
abstains whenever the contract cannot be applied safely.

## Operator review and stale proposals

Dashboard and Telegram review cards show proposed changes, policy version, criteria,
evidence citations, contradictions, and multidimensional confidence. Operators may
approve or reject with reasons. Approval is not an instruction to trust stale data:
the shared service rechecks identity, policy version, source metadata, and expected
current values. A compare-and-swap conflict requires a fresh assessment rather than
forcing the write. Terminal status remains separately protected, including
M8 non-regression. The remaining legacy separation of M8 and Completed is migration debt, not the confirmed target policy.

## Rollout, observability, and evaluation gates

Runtime controls are `PROJECT_STATE_EVIDENCE_GROUNDED_ENABLED`,
`PROJECT_STATE_SHADOW_LEGACY_INFERENCE`,
`PROJECT_STATE_REQUIRE_ASSESSMENT_FOR_MILESTONE_REVIEW`, and
`PROJECT_STATE_CONTRACT_VERSION` in `config/defaults.py`, configured through
`config/settings.py`. During rollout, keep legacy inference in shadow mode only.
Monitor candidate counts, abstention codes, missing criteria, contradictions,
contract/parse failures, and legacy-versus-evaluator disagreements through
`src/project_state/observability.py`.

Deployment gates include the non-live backend suite, coverage, dashboard lint/type
check/build, and `scripts/eval_project_state.py`. Investigate false advancement,
terminal-status false positives, citation accuracy, criterion coverage, identity
accuracy, contradiction detection, abstention quality, and support non-regression.
Do not weaken thresholds to make a rollout pass.

## Troubleshooting

- Missing assessment: confirm extraction produced typed facts and source content is
  available; do not fall back to regex acceptance.
- Contract failure or version mismatch: stop milestone proposal generation, verify
  the configured version and contract validation error, then deploy a supported
  contract.
- Missing or contradicted criterion: obtain eligible cited evidence or reject the
  candidate. Do not reinterpret wiki prose as acceptance.
- Stale/CAS failure: reload project truth and generate a new assessment.
- Shadow disagreement: inspect facts, criteria, and citations; never repair the
  current project automatically.

## Read-only operational audit

`scripts/audit_project_state_inference.py` accepts an explicit database path, opens
SQLite with `mode=ro&immutable=1`, sets `PRAGMA query_only=ON`, enforces positive
row and quote bounds, and emits comparison output only. Identify the path from active
configuration without guessing, record SHA-256 before and after, and require an
identical digest. The audit reports disagreements and missing-contract/source cases;
it performs no repair, approval, migration, or write.

## Safe contract-change procedure

1. Edit the contract SSOT, not prompts, wiki prose, or regexes.
2. Bump the supported policy version in the contract loader and runtime config.
3. Update typed models, contract integrity tests, evaluator tests, and the evaluation
   corpus for the changed criteria and modalities.
4. Run the complete verification gates and a bounded shadow comparison against the
   explicitly configured read-only database.
5. Review disagreements, abstentions, false advancement, citations, and operator
   impact with the policy owner. Never repair historical project state automatically.
6. Deploy only after review approval, retaining rollback through configuration rather
   than autonomous milestone rollback, jump, or revert.
