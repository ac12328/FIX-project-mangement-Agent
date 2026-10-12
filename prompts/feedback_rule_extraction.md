You are extracting a reusable correction rule from operator feedback on an AI-generated output.

The operator has marked an AI output as "wrong" and provided context about what was incorrect.

## Reasoning Protocol (MANDATORY)

You MUST reason step-by-step in <think>...</think> tags BEFORE producing your JSON output:

<think>
Step 1: UNDERSTAND — What specific AI behavior did the operator correct? Quote the key phrase from their note.
Step 2: GENERALIZE — Can this correction apply to future situations beyond this one case? Or is it purely situation-specific?
Step 3: FORMULATE — Draft a concrete, actionable rule. It MUST contain at least one specific noun (FIX tag, system name, field, milestone, process step).
Step 4: DEDUP — Compare against "Existing Rules" below. Is this already covered by an existing rule? If yes → rule_type = "skip".
Step 5: CLASSIFY — Is this about communication style with a specific person/org (response_pattern)? Or a general operational rule (learned_rule)?
</think>

Then output ONLY the JSON result.

## Your Task

Determine whether this feedback yields a reusable rule:
1. If the feedback is **contact-specific** (about communication style with a particular person/org), set rule_type to "response_pattern".
2. If the feedback yields a **general reusable rule** (applies across projects), set rule_type to "learned_rule".
3. If the feedback is too **vague, tautological, or situation-specific** to produce a reusable rule, set rule_type to "skip".

## Quality Requirements for learned_rule

Your extracted rule MUST:
- Contain at least one **concrete noun** (a specific FIX tag, system, field, entity, milestone, or process step)
- Be **actionable** — someone reading it should know exactly what to do
- Be **specific enough** to distinguish right from wrong behavior
- Be max **120 characters**

Your extracted rule MUST NOT:
- Be tautological ("ensure correct assignment", "do the right thing", "assign to the correct project")
- Restate the obvious ("make sure the output is correct")
- Be so broad it applies to everything ("always double-check work")
- Duplicate an existing rule (see "Existing Rules" section below if provided)

If you cannot produce a rule meeting these criteria, use rule_type "skip".

## Output JSON

Return a JSON object:
- `rule_type`: "response_pattern", "learned_rule", or "skip"
- `rule`: The reusable rule text (max 120 chars). Required unless rule_type is "skip".
- `condition`: When this rule applies. E.g., "counterparty matches an operator-provided project scope" or null if always applicable
- `contact_specific`: true if the correction is about how to communicate with a specific person/org
- `confidence`: Float 0.5-0.9. Higher if the operator note is specific and clear. Set 0.0 if skip.
- `pattern_field`: If rule_type is "response_pattern", which field to update: "tone", "preferred_greeting", "avoid_phrases", "preferred_language", "response_length_preference", or null
- `pattern_value`: The value to set for pattern_field, or the phrase to add to avoid_phrases

Return ONLY valid JSON. No markdown, no explanation.
