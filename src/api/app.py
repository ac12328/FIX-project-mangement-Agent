"""
FastAPI application for AgentFIXBPRO dashboard API.
"""

import logging
import threading
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from src.api.routers import (
    dashboard, intake, projects, activity, drafts,
    reviews, evidence, milestones, memory, followups, events, telegram_messages, traces, query,
    reports, preferences, closing, learned_rules,
    portfolio_health, ai_feedback,
    health, llm_analytics, llm_config,
    support, conversation_context, briefs,
    learning, learning_center, learning_center_actions, wiki, timeline,
    metrics, search, attachments, operator_memory, parties, capabilities, auth,
    approvals, work_items, inbox, calendar, fix_sessions, admin_auth, draft_workspace, ui_v2, assistant,
    operator_updates, go_live_deliveries, support_deliveries, inbound_email_work, inbound_presentations, project_advisories,
    contact_identity_cards,
    draft_card_deliveries, personal_reminder_deliveries, draft_outbox_reconciliation,
)
from src.llm.client import enable_tracing
from src.telemetry.setup import init_telemetry
from src.tools.integrity import validate_tool_integrity
from config.settings import (
    CONFIG,
    inject_llm_keys,
    require_valid_config_load,
)

logger = logging.getLogger(__name__)
_standalone_lifespan_lock = threading.Lock()
# An unconfirmed worker drain must not be silently adopted by a later lifespan.
_standalone_shutdown_failed = False


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Ensure a standalone process has exactly one API worker/DB owner."""
    if application.state.runtime_mode == "integrated":
        async with _runtime_lifespan(application):
            yield
        return
    if not _standalone_lifespan_lock.acquire(blocking=False):
        raise RuntimeError("Another standalone API lifespan already owns this process")
    try:
        if _standalone_shutdown_failed:
            raise RuntimeError(
                "Previous standalone API shutdown did not confirm worker drain; restart the process"
            )
        async with _runtime_lifespan(application):
            yield
    finally:
        _standalone_lifespan_lock.release()


@asynccontextmanager
async def _runtime_lifespan(application: FastAPI):
    """Startup: init DB + integrity check. Shutdown: WAL checkpoint + close.

    When running in-process with the bot (single-process mode), DB is already
    initialized by run_bot.py ??we only need to ensure schema is applied.
    """
    from src.db.connection import (
        close_all_connections,
        close_write_pool, scoped_write,
    )
    from src.db.schema import apply_schema
    from src.db.migrations import run_migrations
    from src.email.attachment_recovery import recover_pending

    def verify_db() -> None:
        inject_llm_keys(CONFIG)
        with scoped_write() as conn:
            apply_schema(conn)
            run_migrations(conn)
            if application.state.runtime_mode == "standalone":
                recovered = recover_pending(conn, CONFIG.data_dir)
                if recovered:
                    logger.warning("Reconciled %d draft attachment operations", recovered)
            if CONFIG.dashboard_auth.enabled:
                from src.auth.repository import bootstrap_primary_admin, purge_auth_records
                bootstrap_primary_admin(conn)
                purge_auth_records(conn)
        logger.info("API DB schema verified (migrations applied)")

    # The bot owns process-wide services in integrated mode. Standalone owns
    # them itself; classify borrowed workers before opening its DB scope so a
    # failed schema/recovery startup can still drain and close safely.
    if application.state.runtime_mode == "integrated":
        verify_db()
        yield
        return

    from src.auth.session_touch import (
        session_touch_health,
        start_session_touch_worker,
        stop_session_touch_worker,
    )
    from src.observability.pipeline import (
        start_trace_pipeline,
        stop_trace_pipeline,
        trace_pipeline_health,
    )
    from src.assistant.tool_centric_shadow_worker import (
        start_tool_centric_shadow_worker,
        stop_tool_centric_shadow_worker,
        tool_centric_shadow_health,
    )
    from src.email.project_truth.shadow_worker import (
        email_project_truth_mode,
        email_project_truth_shadow_health,
        start_email_project_truth_shadow_worker,
        stop_email_project_truth_shadow_worker,
    )
    from src.telemetry.setup import force_flush_telemetry

    trace_was_running = bool(trace_pipeline_health().get("worker_alive"))
    session_touch_was_running = bool(session_touch_health().get("running"))
    shadow_was_running = bool(tool_centric_shadow_health().get("running"))
    email_shadow_was_running = bool(
        email_project_truth_shadow_health().get("running")
    )
    owns_db = not any((
        trace_was_running, session_touch_was_running,
        shadow_was_running, email_shadow_was_running,
    ))
    telemetry_attempted = False
    attempted_workers: set[str] = set()
    try:
        verify_db()
        enable_tracing()
        telemetry_attempted = True
        init_telemetry(service_name="agentfixbpro-api")
        attempted_workers.add("trace pipeline")
        start_trace_pipeline()
        attempted_workers.add("session touch")
        start_session_touch_worker()
        attempted_workers.add("tool shadow")
        start_tool_centric_shadow_worker()
        attempted_workers.add("email shadow")
        email_shadow_worker = start_email_project_truth_shadow_worker()
        logger.info(
            "API observability/session workers started; email project truth mode=%s, shadow_worker_running=%s",
            email_project_truth_mode(),
            email_shadow_worker is not None,
        )
        yield
    finally:
        # A pre-existing worker belongs to another runtime owner; never stop it
        # or close its shared DB pool when this API lifespan ends.
        failures: list[str] = []
        owned_stops = (
            ("session touch", not session_touch_was_running, stop_session_touch_worker, 2.0),
            ("tool shadow", not shadow_was_running, stop_tool_centric_shadow_worker, 5.0),
            ("email shadow", not email_shadow_was_running, stop_email_project_truth_shadow_worker, 5.0),
            ("trace pipeline", not trace_was_running, stop_trace_pipeline, 5.0),
        )
        for name, owned, stop, timeout in owned_stops:
            if not owned or name not in attempted_workers:
                continue
            try:
                if stop(timeout=timeout) is not True:
                    failures.append(name)
                    logger.error("Standalone API %s did not confirm worker drain", name)
            except Exception:
                failures.append(name)
                logger.exception("Standalone API %s stop raised", name)
        if not trace_was_running and telemetry_attempted:
            try:
                if force_flush_telemetry(timeout_millis=5000) is not True:
                    failures.append("telemetry flush")
                    logger.error("Standalone API telemetry flush did not confirm completion")
            except Exception:
                failures.append("telemetry flush")
                logger.exception("Standalone API telemetry flush raised")
        if owns_db and not failures:
            for name, close in (("connections", close_all_connections), ("write pool", close_write_pool)):
                try:
                    close()
                except Exception:
                    failures.append(f"DB {name}")
                    logger.exception("Standalone API DB %s close raised", name)
            if not failures:
                logger.info("Standalone API shutdown: workers flushed and DB connections closed")
        elif not owns_db:
            logger.warning("Standalone API borrowed process workers; shared DB pool remains open")
        if failures:
            global _standalone_shutdown_failed
            _standalone_shutdown_failed = True
            raise RuntimeError(
                "Standalone API shutdown did not confirm cleanup: " + ", ".join(failures)
            )


def create_app(*, runtime_mode: Literal["standalone", "integrated"] = "standalone") -> FastAPI:
    """Create the API with explicit process ownership of background services."""
    if runtime_mode not in {"standalone", "integrated"}:
        raise ValueError(f"Unknown API runtime mode: {runtime_mode}")
    require_valid_config_load()
    validate_tool_integrity()
    application = FastAPI(
        title="AgentFIXBPRO Dashboard",
        version="0.1.0",
        docs_url=None if CONFIG.dashboard_auth.production else "/docs",
        openapi_url=None if CONFIG.dashboard_auth.production else "/openapi.json",
        lifespan=lifespan,
    )
    application.state.runtime_mode = runtime_mode

    # Preparation-only loopback origins; no original deployment origins.
    _origins = [
        "http://127.0.0.1:3317",
        "http://127.0.0.1:3318",
    ]
    if CONFIG.dashboard_base_url:
        _origins.append(CONFIG.dashboard_base_url.rstrip("/"))

    @application.middleware("http")
    async def dashboard_auth_guard(request: Request, call_next):
        from fastapi import HTTPException
        from src.api.auth import require_current_actor, validate_csrf
        from src.auth.rbac import permission_for_request, require_permission

        path = request.url.path
        public_paths = {
            "/api/health",
            "/api/auth/config",
            "/api/auth/entra/login",
            "/api/auth/entra/callback",
            "/api/auth/emergency/login",
            "/api/auth/login",
            "/api/auth/register",
            "/api/auth/verify-email",
            "/api/auth/resend-verification",
            "/api/auth/forgot-password",
            "/api/auth/reset-password",
        }
        if request.method == "OPTIONS" or not path.startswith("/api") or path in public_paths:
            return await call_next(request)
        try:
            actor = require_current_actor(request)
            validate_csrf(request, actor)
            permission = permission_for_request(request.method, path)
            if permission:
                require_permission(actor, permission)
            request.state.actor = actor
        except HTTPException as exc:
            return JSONResponse(
                status_code=exc.status_code,
                content={"detail": exc.detail},
            )
        return await call_next(request)

    # Add CORS after the auth middleware so Starlette's outermost middleware
    # decorates auth failures and handles browser preflight requests itself.
    application.add_middleware(
        CORSMiddleware,
        allow_origins=_origins,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=[
            "content-type",
            "authorization",
            "x-requested-with",
            "x-csrf-token",
        ],
        allow_credentials=True,
    )

    application.include_router(auth.router, prefix="/api")
    application.include_router(admin_auth.router, prefix="/api")
    application.include_router(dashboard.router, prefix="/api")
    application.include_router(intake.router, prefix="/api")
    application.include_router(projects.router, prefix="/api")
    application.include_router(fix_sessions.router, prefix="/api")
    application.include_router(activity.router, prefix="/api")
    application.include_router(drafts.router, prefix="/api")
    application.include_router(draft_workspace.router, prefix="/api")
    application.include_router(reviews.router, prefix="/api")
    application.include_router(evidence.router, prefix="/api")
    application.include_router(milestones.router, prefix="/api")
    application.include_router(memory.router, prefix="/api")
    application.include_router(operator_memory.router, prefix="/api")
    application.include_router(followups.router, prefix="/api")
    application.include_router(events.router, prefix="/api")
    application.include_router(telegram_messages.router, prefix="/api")
    application.include_router(traces.router, prefix="/api")
    application.include_router(query.router, prefix="/api")
    application.include_router(reports.router, prefix="/api")
    application.include_router(preferences.router, prefix="/api")
    application.include_router(closing.router, prefix="/api")
    application.include_router(learned_rules.router, prefix="/api")
    application.include_router(portfolio_health.router, prefix="/api")

    application.include_router(ai_feedback.router, prefix="/api")
    application.include_router(health.router, prefix="/api")
    application.include_router(llm_analytics.router, prefix="/api")
    application.include_router(llm_config.router, prefix="/api")
    application.include_router(support.router, prefix="/api")
    application.include_router(conversation_context.router, prefix="/api")
    application.include_router(briefs.router, prefix="/api")
    application.include_router(learning.router, prefix="/api")
    application.include_router(learning_center.router, prefix="/api")
    application.include_router(learning_center_actions.router, prefix="/api")
    application.include_router(wiki.router, prefix="/api")
    application.include_router(timeline.router, prefix="/api")
    application.include_router(metrics.router, prefix="/api")
    application.include_router(search.router, prefix="/api")
    application.include_router(attachments.router, prefix="/api")
    application.include_router(parties.router, prefix="/api")
    application.include_router(capabilities.router, prefix="/api")
    application.include_router(approvals.router, prefix="/api")
    application.include_router(work_items.router, prefix="/api")
    application.include_router(inbox.router, prefix="/api")
    application.include_router(calendar.router, prefix="/api")
    application.include_router(ui_v2.router, prefix="/api")
    application.include_router(assistant.router, prefix="/api")
    application.include_router(operator_updates.router, prefix="/api")
    application.include_router(go_live_deliveries.router, prefix="/api")
    application.include_router(personal_reminder_deliveries.router, prefix="/api")
    application.include_router(draft_card_deliveries.router, prefix="/api")
    application.include_router(support_deliveries.router, prefix="/api")
    application.include_router(inbound_presentations.router, prefix="/api")
    application.include_router(project_advisories.router, prefix="/api")
    application.include_router(contact_identity_cards.router, prefix="/api")
    application.include_router(inbound_email_work.router, prefix="/api")
    application.include_router(draft_outbox_reconciliation.router, prefix="/api")
    from src.api.routers import update_notifications
    application.include_router(update_notifications.router, prefix="/api")
    from src.api.routers import followup_plans
    application.include_router(followup_plans.router, prefix="/api")

    return application


app = create_app()
