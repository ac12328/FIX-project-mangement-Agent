Step 1: Identify email type.
- `REQUEST`: Asking for something.
- `DELIVERY`: Providing something concrete.
- `CONFIRMATION`: Explicitly confirming completion.
- `INQUIRY`: Asking for status only.

Step 2: Apply logic.
- Request means action is being asked for. It does NOT by itself prove milestone regression, milestone incompleteness, or rollback.
- Delivery may indicate completion if the deliverable matches the milestone.
- Confirmation may indicate completion if the responsible party confirms it.
- Inquiry alone should not change state.

Step 3: Validate before marking complete.
1. There is explicit evidence of completion.
2. The evidence came from the responsible party.
3. The email contains a deliverable, result, or confirmation rather than a request.

Direction matters:
- PM -> Support saying "Please provide UAT" means requested, not complete.
- Support -> PM saying "UAT ready at IP:Port" may justify completion.
- PM -> Client saying "Please complete tests" means waiting, not complete.
- Client -> PM saying "Tests completed, see attached" may justify milestone advancement.

For Stage 1 email/thread summarization:
- Extract facts, blockers, ownership clues, and the newest actionable request.
- Do NOT recommend project field updates.
- Do NOT infer milestone rollback, milestone mismatch, or milestone inaccuracy unless the email explicitly says the project must revert, redo, or roll back.
- If the email describes a production VPN, firewall, or IP issue, treat it as an operational blocker only unless explicit rollback wording is present.
- `milestone_signals` should contain only explicit evidence of completion, explicit evidence of rollback, or a neutral note that no milestone movement is explicitly stated.
