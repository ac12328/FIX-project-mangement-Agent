# Public Demo Policy and Publication Limits

Prepared on 2026-10-12. This is a de-identified preparation edition, not a production deployment or a claim of publication clearance.

## Provenance
The explanatory Wiki pages and blank templates were independently authored for this edition. They are draft, low-confidence documentation, not externally verified standards, operational evidence, or approval records. The versioned JSON contract remains the executable policy owner. No real case was relabeled or date-shifted into a fictional case. No new client events, FIX traces, CompIDs, certificates, market rules or supporting PDFs were invented.

## Separate authorities
| Capability | Demo Provisioning Support | Demo Connectivity Acceptance Support |
|---|---|---|
| Approve mandatory conformance-case exemptions | Yes | No |
| Confirm production-session information delivery | Yes | No |
| Approve per-step PCT scope exemptions | Yes | Yes |
| Confirm PCT completion | Yes | Yes |

The exact acceptance domain is acceptance.demo.invalid. A separate exact mailbox exception is acceptance-special@exception.demo.invalid. Other mailboxes on the exception domain are not granted authority. Subdomains and lookalikes are not authorized by the acceptance-domain rule. Directory bindings still require operator confirmation and retain conflict checks. Sender-role authorization is not SMTP/DKIM authentication.

## Safety and compatibility
Eight milestone stages, evidence thresholds, review gates, no-regression rules, transactional outbox delivery and uncertain-send reconciliation are retained. Identifier changes are coordinated across Python, SQLite definitions/migrations, contract keys, TypeScript and API types. This edition is fresh-database-only: no production database, history or identity migration is provided. Do not connect it to an existing deployment.

Runtime launchers and service controls are blocked. No credentials, admin identity or Telegram destination is prefilled. Incoming integrations are disabled in the template. Reserved endpoints and an independent single-demo-vendor profile are placeholders, not a transformed real roster. Windows-profile credential hydration is disabled; deployment-specific environment names are isolated under AGENTFIX_DEMO_. Standard provider environment variables still exist in library integration code; use a clean process environment for any future isolated deployment.

## Knowledge omitted
The inherited Python/dashboard tests and fixtures are withheld intact pending independent synthetic-fixture review; not every withheld file is asserted to contain sensitive data. Private historical cases, operator learning snapshots, replay/backfill/seed tooling, internal creation-policy prose, private reference catalogs, institutional UAT checklists/endpoints/credentials and captured UI artifacts are not distributed. Approved draft-grounding paths are empty, and the institutional UAT draft validator fails closed rather than inventing a replacement checklist. The original institutional Wiki corpus is not included. Draft prose is limited to generic workflow boundaries; private market-specific requirements, routing lists and procedures are not reconstructed.

## Open release decisions
- The M7 short-form completion fallback still needs modality review; the static concern is not a runtime reproduction or an arbitrary-sender bypass.
- M5 criterion-level L4 and summary-level L3 wording disagree; existing evaluator behavior is not changed here.
- The stage-default advisory boolean and prose disagree; neither is silently corrected.
- Some retained prompt/legacy comments may be stale relative to the executable contract. This edition does not certify policy coherence or complete duplicate-scope enforcement.
- TLS setup guidance is procedural; universal technical enforcement is not established.
- No full Python product suite, dashboard build, browser test or service smoke test is implied by de-identification.
- Ownership, third-party document rights, institutional know-how clearance and a chosen license remain owner decisions. No license is added automatically.

Private reverse mappings, before/after hashes and the excluded originals are kept outside this public tree. The earlier de-identification pass did not initialize or push Git. The owner separately authorized preparation-snapshot publication on 2026-10-12; this does not resolve the open release decisions or certify runtime readiness.
