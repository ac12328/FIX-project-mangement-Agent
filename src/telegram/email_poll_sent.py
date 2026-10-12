"""Sent-items synchronization at the scheduled email poll boundary."""
from __future__ import annotations

from src.telegram.email_poll_ports import EmailPollPorts


def sync_sent_items(sent_result, batch_limit: int, ports: EmailPollPorts) -> None:
    """Persist each outbound copy before publishing its ticket/project refresh."""
    from src.email.email_processor import process_sent_copy, publish_project_email_processed
    from src.email.thread_linker import try_thread_link, publish_thread_linked

    if sent_result.error:
        ports.logger.warning("Email poll sent-items error: %s", sent_result.error)
        return
    sent_to_process = sent_result.fetched[:batch_limit]
    if len(sent_result.fetched) > batch_limit:
        ports.logger.info(
            "Email poll: %d sent items fetched, processing first %d this cycle",
            len(sent_result.fetched), batch_limit,
        )
    for fetched in sent_to_process:
        with ports.scoped_write() as conn:
            project_id = process_sent_copy(conn, fetched)
            # Ticket link and outbound copy share the caller-owned transaction.
            sent_ticket = try_thread_link(
                conn, fetched, is_outbound=True, defer_commit=True,
            )
        if sent_ticket:
            publish_thread_linked(sent_ticket.ticket_id)
        if project_id:
            publish_project_email_processed(
                fetched, project_id=project_id,
                classification="sent_outbound", direction="outbound",
            )
    if sent_to_process:
        ports.logger.info("Email poll: synced %d sent item(s)", len(sent_to_process))
