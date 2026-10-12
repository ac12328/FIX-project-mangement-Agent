# Skill: portfolio

Cross-project portfolio views.

- query_projects, list_work_items, light follow-up overview
- For creating/completing follow-ups prefer skill `followup` when the host re-delegates

## Outstanding / open projects

When the operator asks 「有冇 outstanding」「邊啲仲 open」「any projects outstanding?」「what is outstanding」:

1. Call `query_projects` with **no** `project_status` filter (and optional `order_by_staleness=true`).
2. Default result = **Active + Blocked + Waiting** (all still open work).
3. **FORBIDDEN:**
   - `project_status=Active` only → hides Blocked/Waiting
   - Summarizing **only** Blocked/Waiting and skipping Active → **wrong**
4. Your reply MUST:
   - State total count and breakdown (e.g. Active=10, Blocked=4, Waiting=1)
   - List **every** project returned by the tool (or say truncated if tool truncated)
   - Include a short **problem / next_action** per row when the tool provides `next:`
5. Flag External ball + stale, and M8 still open (residual) separately.
6. Completed/Archived are not outstanding unless user asks for closed (`include_all=true`).
