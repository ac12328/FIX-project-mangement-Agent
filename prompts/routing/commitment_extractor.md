# Commitment Extractor

You are analysing a client email for a FIX protocol onboarding project.

Determine whether the email contains a concrete commitment — something the client
promises to do (e.g. "we will send the config by Friday", "our team will complete
UAT next week").

If yes, extract:
- **commitment_text**: the exact commitment quote or paraphrase
- **due_date**: any due date mentioned (ISO 8601 format, e.g. 2026-04-10)

If no date is stated, set due_date to null.
If no commitment is found, set has_commitment to false.
