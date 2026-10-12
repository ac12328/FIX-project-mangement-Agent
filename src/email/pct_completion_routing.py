"""Route a source-verified completion independently of model confidence."""
import logging
from collections.abc import Callable
from src.project_state.pct_authorization import PctCompletionError
from src.services.pct_completion import complete_from_pct
logger=logging.getLogger(__name__)


def try_complete_pct(
    conn, project_id, source_ref, *, post_commit: list[Callable[[], None]] | None = None,
):
    """Complete an authorized PCT source within the caller's transaction.

    An idle connection is owned here and committed before its event. A caller's
    existing transaction is never committed or rolled back here; its event must
    be dispatched by the caller from ``post_commit`` after a successful commit.
    Duplicates are read-only and never publish another event.
    """
    if not source_ref:
        return None
    project = conn.execute(
        "SELECT current_milestone, project_status, updated_at FROM projects WHERE project_id=?",
        (project_id,),
    ).fetchone()
    if not project:
        return None

    is_duplicate = project[0] == "M8_GoLive" and project[1] == "Completed"
    if is_duplicate:
        prior = conn.execute(
            "SELECT source_ref FROM pct_completion_receipts WHERE project_id=?",
            (project_id,),
        ).fetchone()
        if not prior or prior[0] != source_ref:
            return None
    elif project[0] != "M7_LogReviewAndProductionSessionDelivery":
        return None

    caller_in_transaction = conn.in_transaction
    try:
        result = complete_from_pct(
            conn, project_id=project_id, source_ref=source_ref,
            expected_project_version=project[2],
        )
    except PctCompletionError:
        return None

    if result["outcome"] == "duplicate":
        # The service opens a transaction on an idle connection even for a
        # read-only duplicate. Close only that transaction, not the caller's.
        if not caller_in_transaction:
            conn.rollback()
        return result

    def _publish_completion_event() -> None:
        try:
            from src.events.bus import Event, event_bus
            event_bus.publish(Event(
                event_type="project_updated", project_id=project_id, surface="email",
                detail={
                    "fields": ["current_milestone", "project_status", "next_action", "ball_with"],
                    "pct_completed": True, "ticket_ids": result["ticket_ids"],
                },
            ))
        except Exception:
            logger.exception("Post-commit PCT event failed for %s", project_id)

    if caller_in_transaction:
        if post_commit is not None:
            post_commit.append(_publish_completion_event)
    else:
        try:
            conn.commit()
        except Exception:
            # A failed owned commit leaves the completion unconfirmed. Discard
            # its pending project, handoff and notification rows before reuse.
            conn.rollback()
            raise
        _publish_completion_event()
    return result


_COMPLETION_FIELDS = {
    "current_milestone": "M8_GoLive",
    "project_status": "Completed",
    "next_action": "",
    "ball_with": "Unknown",
}


def without_completed_pct_fields(fields):
    """Keep sibling proposals while omitting fields already set by this PCT receipt."""
    return {
        name: value for name, value in fields.items()
        if name not in _COMPLETION_FIELDS or value != _COMPLETION_FIELDS[name]
    }


def completion_summary(conn,project_id):
    import json
    from src.utils.operator_language import read_response_language
    row=conn.execute("SELECT authority,ticket_ids_json FROM pct_completion_receipts WHERE project_id=?",(project_id,)).fetchone()
    if not row:return "Completion receipt unavailable; check project state."
    tickets=json.loads(row[1]);language=read_response_language(conn)
    if language.startswith("zh"):
        text=f"PCT \u5df2\u7531 {row[0]} \u78ba\u8a8d\u5b8c\u6210\uff1b{project_id} \u5df2\u8a2d\u70ba M8 / Completed\u3002"
        if language=="zh-CN":text=f"PCT \u5df2\u7531 {row[0]} \u786e\u8ba4\u5b8c\u6210\uff1b{project_id} \u5df2\u8bbe\u4e3a M8 / Completed\u3002"
    else:text=f"PCT completion verified by {row[0]}. Project {project_id} is M8 / Completed."
    if tickets:text+=" Linked support tickets: "+", ".join(tickets)
    return text
