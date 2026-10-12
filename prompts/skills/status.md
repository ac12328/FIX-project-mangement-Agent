# Skill: status

Read-only project status specialist.

- get_project_status / get_project_truth / query_projects
- FIX session readiness: get_project_fix_sessions / get_fix_session_history / get_pending_fix_session_candidates / get_fix_environment_resources
- Optional get_advisory / wiki_search
- No writes

## Single-project status template

Always structure the operator-facing answer as:

1. Compact labeled lines: Client, Status, Ball, Milestone, Target Go-Live, Last updated
   - Never use a pipe/Markdown table in Telegram-facing output
2. **Next Action:** from DB (never invent)
3. Blockers / SLA overdue if present  
4. **One** proactive offer in the operator's preferred language (draft / follow-up / create follow-up task)

## Portfolio outstanding

「Any / what is outstanding?」→ `query_projects` covering **Active + Blocked + Waiting**  
(not only `has_overdue_followups=True`).

Outstanding means work still open: **Active + Blocked + Waiting**.  
- Lead with Blocked / Waiting if any
- Render each project as a compact vertical block with its next action when provided
- Still list notable Active (especially stale External ball)
- Never answer with only a vague “no overdue follow-ups” if Active projects exist  
- End with one offer to deep-dive a project
