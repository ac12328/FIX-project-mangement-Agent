"""Email polling job with explicit call-time dependencies and unchanged delivery policy."""
from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from src.telegram.email_poll_ports import EmailPollPorts
from src.telegram.email_poll_batch_capture import capture_poll_batch
from src.telegram.email_poll_discovery import classify_and_flush_discovery


async def run_email_poll(context, ports: EmailPollPorts) -> None:
    data = context.job.data
    chat_id = data["chat_id"]
    try:
        async def _core():
            from config import defaults
            from config.settings import CONFIG
            from src.email.email_processor import (
                classify_fetched_email,
                publish_project_email_processed,
            )
            from src.email.inbound_work import (
                ImapSource, capture_imap_batch, claim_pending_route,
                finish_route, require_reconciliation, checkpoint_route_step,
                RouteClaimExpectation, RouteDeliveryIntent,
            )
            from src.email.source_capture import imap_source_receipt
            from src.email.inbound_recovery import recover_pending_imap
            from src.email.inbox_fetcher import ImapInboxFetcher
            from src.email.thread_linker import try_thread_link, publish_thread_linked
            from src.telegram.email_poll_delivery import (
                InboundRouteClaim, prepare_thread_ticket_notice,
                stage_thread_ticket_notice, send_completed_thread_ticket_notice,
                flush_overflow_card,
            )
            from src.telegram.email_poll_sent import sync_sent_items
            from src.telegram.email_poll_image_evidence import route_image_evidence
            from src.telegram.email_poll_inbox_intelligence import route_inbox_intelligence
            from src.email.ticket_notice import is_delivery_failure
            from src.repositories.operator_state_repo import get_notification_mode
            from src.utils.push_guard import URGENCY_NORMAL
            from src.utils.working_hours import should_notify
            from src.wiki.domain_index import get_wiki_domain_index

            with ports.scoped_read() as conn:
                _should_send_cards = should_notify(CONFIG, get_notification_mode(conn), URGENCY_NORMAL)
                from src.telegram.email_poll_limits import resolve_email_poll_limits
                email_batch_limit, persist_batch_size = resolve_email_poll_limits(conn)

            imap_cfg = CONFIG.email.imap
            fetcher = ImapInboxFetcher.from_config(imap_cfg)

            with ports.scoped_read() as conn:
                inbox_result = fetcher.fetch_inbox(conn, mark_seen=True)
            if inbox_result.error:
                ports.logger.warning("Email poll inbox error: %s", inbox_result.error)
            seen_in_batch: set = set()
            # D2: Card batching ??send individual cards up to threshold,
            # then collect overflow as a single summary card
            CARD_BATCH_LIMIT = 3
            cards_sent_this_cycle = 0
            overflow_summaries: list[str] = []
            # Limit emails processed per cycle to prevent write-lock starvation.
            # Remaining emails will be caught by the next poll cycle (watermark
            # only advances to the max UID we actually process).
            uid_validity = getattr(inbox_result, "uid_validity", None)
            verified_source = (
                ImapSource.from_fetcher(fetcher, uid_validity)
                if type(uid_validity) is int and uid_validity > 0 else None
            )
            if inbox_result.fetched and verified_source is None:
                ports.logger.error("IMAP UIDVALIDITY unavailable; refusing to anchor or route inbox mail")
            emails_to_process = (
                inbox_result.fetched[:email_batch_limit]
                if verified_source and not inbox_result.error else []
            )
            if len(inbox_result.fetched) > email_batch_limit:
                ports.logger.info(
                    "Email poll: %d emails fetched, processing first %d this cycle "
                    "(persist_batch=%d)",
                    len(inbox_result.fetched), email_batch_limit, persist_batch_size,
                )
            max_processed_uid: int | None = None
            consecutive_lock_failures = 0
            capture_failed = False

            async def _process_after_persist(
                fetched, event, is_new, *, route_token=None,
            ) -> None:
                nonlocal max_processed_uid, cards_sent_this_cycle
                # Recovery may revisit an older UID after the discovery mark moved.
                # Only fresh discovery can contribute to this cycle's watermark.
                if route_token is None and fetched.uid is not None:
                    if max_processed_uid is None or fetched.uid > max_processed_uid:
                        max_processed_uid = fetched.uid
                if fetched.email_id in seen_in_batch:
                    return
                seen_in_batch.add(fetched.email_id)
                if not is_new:
                    if fetched.uid is not None:
                        from src.email.inbox_fetcher import cache_processed_uid
                        cache_processed_uid(fetched.uid)
                    ports.logger.info(
                        "Email poll: %s already processed ??skipping route/notify",
                        fetched.email_id,
                    )
                    return
                if route_token is None:
                    expected = RouteClaimExpectation(
                        source_type="imap", account_key=verified_source.account_key,
                        mailbox=verified_source.mailbox,
                        uid_validity=verified_source.uid_validity,
                        source_uid=fetched.uid,
                        classification_json=json.dumps(asdict(event), ensure_ascii=False),
                        checkpoint_json=json.dumps({"source_capture": imap_source_receipt(
                            fetched, verified_source,
                        ).model_dump()}, sort_keys=True), event=event,
                    )
                    with ports.scoped_write() as conn:
                        route_token = claim_pending_route(conn, fetched.email_id, expected)
                    if route_token is None:
                        # A changed capture/anchor cannot be routed from this observation.
                        # Only a still-pending row may be quarantined; another claimant
                        # must retain ownership of its already-started route.
                        with ports.scoped_write() as conn:
                            quarantine_token = claim_pending_route(conn, fetched.email_id)
                            if quarantine_token is not None:
                                require_reconciliation(
                                    conn, fetched.email_id, quarantine_token,
                                    reason="fresh IMAP claim snapshot changed",
                                )
                        return
                overflow_start = len(overflow_summaries)
                pending_ticket_notice = None
                pending_thread_ticket = None
                pending_thread_event_id = None
                route_failed = False
                route_failure_reason = "route exception; committed effects unknown"

                def _checkpoint_route_step(
                    step: str, status: str, *,
                    delivery_intent: RouteDeliveryIntent | None = None,
                    delivery_message_ids: list[int] | None = None,
                ) -> None:
                    with ports.scoped_write() as conn:
                        delivery = {}
                        if delivery_intent is not None:
                            delivery["delivery_intent"] = delivery_intent
                        if delivery_message_ids is not None:
                            delivery["delivery_message_ids"] = delivery_message_ids
                        if not checkpoint_route_step(
                            conn, fetched.email_id, route_token, step=step, status=status,
                            **delivery,
                        ):
                            raise RuntimeError(
                                f"Inbound {step} checkpoint lost for {fetched.email_id}"
                            )
                # Phase 2: Route + Notify (best-effort ??do NOT delete from
                # processed_emails on failure, as notifications may already
                # have been sent to the operator).
                try:
                    # Suppress before thread linking or project-progress previews.
                    if is_delivery_failure(fetched.sender_email, fetched.subject, fetched.body):
                        _checkpoint_route_step("policy_route", "started")
                        await asyncio.to_thread(
                            ports.route_inbound_event, None, event, fetched.body, fetched=fetched,
                        )
                        _checkpoint_route_step("policy_route", "completed")
                        if fetched.uid is not None:
                            from src.email.inbox_fetcher import cache_processed_uid
                            cache_processed_uid(fetched.uid)
                        return
                    # ???? Thread-based ticket auto-linking ????????????????????????????????????????????
                    if (getattr(defaults, "PERSONAL_ASSISTANT_INBOX_INTELLIGENCE_ENABLED", False)
                            and not await route_inbox_intelligence(
                                fetched, event, route_token,
                                scoped_write=ports.scoped_write, logger=ports.logger,
                            )):
                        route_failed = True
                        route_failure_reason = "inbox intelligence incomplete; committed effects unknown"
                    if not await route_image_evidence(
                        fetched, event, route_token,
                        scoped_write=ports.scoped_write, logger=ports.logger,
                    ):
                        route_failed = True
                        route_failure_reason = "image evidence incomplete; effects unknown"
                    _checkpoint_route_step("thread_link", "started")
                    with ports.scoped_write() as conn:
                        thread_ticket = try_thread_link(conn, fetched, defer_commit=True)
                        if not checkpoint_route_step(
                            conn, fetched.email_id, route_token,
                            step="thread_link", status="completed",
                        ):
                            raise RuntimeError(
                                f"Inbound thread_link checkpoint lost for {fetched.email_id}"
                            )
                    if thread_ticket:
                        # The local link commit is not the route's final receipt.
                        pending_thread_event_id = thread_ticket.ticket_id
                    # Project review is a separate, potentially committing command.
                    # A failed receipt still allows support notification, but cannot
                    # certify the complete route or run a second reply agent.
                    if (event.classification_type in {"internal", "project_support", "support_issue"}
                            and event.project_id and not event.requires_operator_triage):
                        from src.email.project_support_evidence import preview_project_support_email

                        _checkpoint_route_step("project_preview", "started")
                        try:
                            preview_result = await asyncio.to_thread(
                                preview_project_support_email, event, chat_id=str(chat_id),
                            )
                        except Exception:
                            route_failed = True
                            route_failure_reason = "project preview failed; committed effects unknown"
                            _checkpoint_route_step("project_preview", "incomplete")
                            ports.logger.exception("Project support preview failed for %s", fetched.email_id)
                        else:
                            if (preview_result is not None
                                    and getattr(preview_result, "status", None)
                                    not in {"needs_review", "no_change"}):
                                route_failed = True
                                route_failure_reason = "project preview returned an unsuccessful receipt"
                                _checkpoint_route_step("project_preview", "incomplete")
                                ports.logger.error(
                                    "Project support preview for %s returned %s",
                                    fetched.email_id, getattr(preview_result, "status", None),
                                )
                            else:
                                _checkpoint_route_step("project_preview", "completed")
                    _route_as_project_update = ports.prefer_project_update_routing(event)
                    if thread_ticket and _route_as_project_update:
                        ports.logger.info(
                            "Thread-linked email %s ??ticket %s; "
                            "routing as project_update for %s",
                            fetched.email_id,
                            thread_ticket.ticket_id,
                            event.project_id,
                        )
                    elif thread_ticket:
                        ports.logger.info(
                            "Thread-linked email %s ??ticket %s",
                            fetched.email_id, thread_ticket.ticket_id,
                        )
                        # The thread route bypasses the later policy-card guard. An
                        # incomplete enrichment/preview must not send an ordinary
                        # ticket update or queue it to a routine digest.
                        if not route_failed:
                            pending_ticket_notice = await prepare_thread_ticket_notice(
                                ports, fetched, thread_ticket,
                                should_send_cards=_should_send_cards,
                                cards_sent=cards_sent_this_cycle,
                                card_batch_limit=CARD_BATCH_LIMIT,
                                overflow_summaries=overflow_summaries,
                            )
                            pending_thread_ticket = thread_ticket
                        return

                    def _do_route(_event=event, _body=fetched.body, _fetched=fetched):
                        return ports.route_inbound_event(
                            None, _event, _body, fetched=_fetched,
                            route_claim=InboundRouteClaim(email_id=_fetched.email_id, token=route_token),
                        )

                    _checkpoint_route_step("policy_route", "started")
                    notification = await asyncio.to_thread(_do_route)
                    _checkpoint_route_step(
                        "policy_route",
                        "incomplete" if notification and notification.get("route_requires_reconciliation")
                        else "completed",
                    )
                    if notification and notification.get("route_requires_reconciliation"):
                        route_failed = True
                        route_failure_reason = "route application reported failure; committed effects unknown"
                        ports.logger.error(
                            "Email poll: application reported failure for %s; "
                            "committed effects require reconciliation", fetched.email_id,
                        )
                    # Earlier image/insight/preview work may have failed even when
                    # the policy route itself returns a normal notification. Do not
                    # present that notification as a complete success. An explicit
                    # reconciliation notice is still safe to deliver.
                    if (notification and _should_send_cards and
                            (not route_failed or notification.get("route_requires_reconciliation"))):
                        if cards_sent_this_cycle < CARD_BATCH_LIMIT:
                            _email_summary = "" if notification.get("delivery_failure") else await asyncio.to_thread(
                                ports._summarize_email_for_card,
                                fetched.subject,
                                fetched.sender_email,
                                fetched.body,
                            )
                            card = ports.build_card(
                                "B",
                                notification["title"],
                                notification["body"] + ("\n\n" + _email_summary[:1000] if _email_summary else ""),
                                project_id=notification.get("project_id"),
                                extra_buttons=[["DETAILS"]] if notification.get("delivery_failure") else notification.get("buttons"),
                            )
                            card_text = ports.format_card_text(card)
                            _checkpoint_route_step(
                                "operator_card", "started",
                                delivery_intent=RouteDeliveryIntent.for_message(chat_id, card_text),
                            )
                            message_ids = await ports.send_persisted_message(
                                context.bot,
                                chat_id=chat_id,
                                text=card_text,
                                project_id=notification.get("project_id"),
                                message_kind="card",
                                reply_markup=ports.card_to_markup(card),
                                object_links=[{
                                    "object_type": "email",
                                    "object_ref": event.email_id,
                                    "project_id": notification.get("project_id"),
                                }],
                                metadata={"card_profile": "B", "email_id": fetched.email_id, "email_summary": _email_summary,
                                          **notification.get("notification_metadata", {})},
                                require_persistence=True,
                            )
                            _checkpoint_route_step(
                                "operator_card", "completed", delivery_message_ids=message_ids,
                            )
                            cards_sent_this_cycle += 1
                        else:
                            overflow_summaries.append(
                                f"???{notification['title']}: {notification.get('body', '')[:80]}"
                            )
                    _sender_domain = (
                        fetched.sender_email.split("@")[-1].lower()
                        if fetched.sender_email and "@" in fetched.sender_email
                        else ""
                    )
                    _is_internal = get_wiki_domain_index().is_internal(_sender_domain)
                    if not route_failed and ports.should_run_inbound_reply_agent(
                        event,
                        notification=notification,
                        has_in_reply_to=bool(fetched.in_reply_to),
                        is_internal_sender=_is_internal,
                    ):
                        _checkpoint_route_step("reply_agent", "started")
                        try:
                            await ports._process_inbound_reply_via_agent(
                                fetched, event, context, chat_id, _should_send_cards,
                                claim=InboundRouteClaim(email_id=fetched.email_id, token=route_token),
                            )
                            _checkpoint_route_step("reply_agent", "completed")
                        except Exception:
                            # The policy route may already have committed, and
                            # the reply agent may have committed before failing.
                            # The entire route cannot be certified complete.
                            route_failed = True
                            route_failure_reason = (
                                "inbound reply processing failed; committed effects unknown"
                            )
                            _checkpoint_route_step("reply_agent", "incomplete")
                            ports.logger.exception(
                                "Failed to analyse/push inbound reply for %s", event.email_id
                            )
                    if fetched.uid is not None:
                        from src.email.inbox_fetcher import cache_processed_uid
                        cache_processed_uid(fetched.uid)
                except Exception:
                    route_failed = True
                    ports.logger.exception(
                        "Email poll: routing failed for %s from %s (subject: %s) ??"
                        "will NOT retry to avoid duplicate notifications",
                        fetched.email_id,
                        fetched.sender_email,
                        (fetched.subject or "")[:80],
                    )
                except BaseException:
                    # Cancellation or shutdown can interrupt a committed command
                    # before its receipt. Never certify the whole route as done.
                    route_failed = True
                    route_failure_reason = "route interrupted; committed effects unknown"
                    raise
                finally:
                    try:
                        with ports.scoped_write() as conn:
                            if route_failed:
                                require_reconciliation(
                                    conn, fetched.email_id, route_token,
                                    reason=route_failure_reason,
                                )
                            else:
                                from src.email.inbound_overflow import enqueue_overflow_item
                                for summary in overflow_summaries[overflow_start:]:
                                    enqueue_overflow_item(
                                        conn, fetched.email_id, route_token, chat_id, summary,
                                    )
                                from src.email.support_effects import release_imap_support_digests
                                release_imap_support_digests(conn, fetched.email_id, route_token)
                                if pending_ticket_notice is not None:
                                    stage_thread_ticket_notice(
                                        conn, InboundRouteClaim(email_id=fetched.email_id, token=route_token),
                                        fetched, pending_thread_ticket, pending_ticket_notice,
                                        chat_id=chat_id,
                                    )
                                completed = finish_route(conn, fetched.email_id, route_token)
                                if not completed:
                                    raise RuntimeError("IMAP final route claim lost")
                    except Exception:
                        # The item and route completion roll back together. Any
                        # earlier domain/send effects still need operator review.
                        try:
                            with ports.scoped_write() as conn:
                                require_reconciliation(
                                    conn, fetched.email_id, route_token,
                                    reason="route completion or overflow intent failed; effects unknown",
                                )
                        except Exception:
                            ports.logger.exception(
                                "Failed to mark inbound route %s for reconciliation",
                                fetched.email_id,
                            )
                        raise
                    if not route_failed and completed:
                        if pending_thread_event_id is not None:
                            publish_thread_linked(pending_thread_event_id)
                        if pending_ticket_notice is not None and pending_ticket_notice.card is not None:
                            await send_completed_thread_ticket_notice(
                                context, ports, chat_id, fetched, pending_thread_ticket,
                                InboundRouteClaim(email_id=fetched.email_id, token=route_token),
                                pending_ticket_notice,
                            )
                            cards_sent_this_cycle += 1
                        publish_project_email_processed(fetched, event)

            async def _flush_persist_batch(batch: list) -> bool:
                """Persist accumulated classify results. Returns False if cycle should abort."""
                nonlocal consecutive_lock_failures, capture_failed
                if not batch:
                    return True
                outcome = capture_poll_batch(
                    batch, source=verified_source, scoped_write=ports.scoped_write,
                    capture=capture_imap_batch, logger=ports.logger,
                    consecutive_lock_failures=consecutive_lock_failures,
                )
                consecutive_lock_failures = outcome.consecutive_lock_failures
                capture_failed = capture_failed or outcome.failed
                if outcome.persisted is None:
                    return not outcome.abort_cycle
                persisted = outcome.persisted
                await asyncio.sleep(0)

                # Delivery reports need only persistence and policy audit. Skip
                # enrichment too, since suppressed insights can enqueue digests.
                remaining = []
                for fetched, event, is_new in persisted:
                    if is_delivery_failure(fetched.sender_email, fetched.subject, fetched.body):
                        await _process_after_persist(fetched, event, is_new)
                    else:
                        remaining.append((fetched, event, is_new))
                persisted = remaining

                for fetched, event, is_new in persisted:
                    await _process_after_persist(fetched, event, is_new)
                return True

            # Persisted work does not depend on watermark discovery succeeding.
            await recover_pending_imap(
                fetcher, scoped_read=ports.scoped_read,
                scoped_write=ports.scoped_write,
                process=_process_after_persist, logger=ports.logger,
                limit=email_batch_limit,
            )
            classification_failed = await classify_and_flush_discovery(
                emails_to_process, ports=ports, classify=classify_fetched_email,
                persist_batch_size=persist_batch_size, flush_batch=_flush_persist_batch,
            )
            capture_failed = capture_failed or classification_failed
            if inbox_result.fetched:
                ports.logger.info("Email poll: %d new, %d skipped", len(inbox_result.fetched), inbox_result.skipped_count)

            # Advance watermark AFTER processing ??only to the max UID we
            # actually processed (not all fetched), so unprocessed emails
            # are retried in the next poll cycle.
            _wm_uid = max_processed_uid if max_processed_uid is not None else inbox_result.max_uid
            if not inbox_result.error and not capture_failed and verified_source and _wm_uid is not None:
                fetcher.advance_watermark(_wm_uid, uid_validity=uid_validity)

            # Persist UID caches to disk so restarts don't re-scan
            from src.email.inbox_fetcher import save_uid_cache
            save_uid_cache()

            await flush_overflow_card(
                context, ports, chat_id, overflow_summaries,
                should_send_cards=_should_send_cards,
            )

            with ports.scoped_read() as conn:
                sent_result = fetcher.fetch_sent(conn)
            sync_sent_items(sent_result, email_batch_limit, ports)

        await ports.with_async_retry(_core, max_retries=1, delay_seconds=60, job_name="email_poll")
        ports._mark_job_success("email_poll")
    except Exception as exc:
        ports.logger.exception("Failed to run email inbox poll")
        await ports._notify_job_failure(context, chat_id, "email_poll", exc)

