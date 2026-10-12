"""
Entry point to run the AgentFIXBPRO Telegram bot + API server in a single process.

Merging both services into one process eliminates cross-process SQLite write
contention — the WriteSerializer's Python Lock serializes ALL writes from
bot, scheduler, and API threads safely.

Usage: py scripts/run_bot.py
"""

import atexit
import logging
import signal
import sys
import threading
import time
from pathlib import Path

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).parent.parent))

# No inherited Windows-profile credential hydration in this preparation copy.

if __name__ == "__main__":
    raise SystemExit(
        "Runtime launch is disabled in this public preparation edition. "
        "Complete the isolated deployment and publication review first."
    )
_HYDRATED_RUNTIME_ENV = ()

from src.utils.log_setup import configure_logging
configure_logging("bot")
logger = logging.getLogger("run_bot")
if _HYDRATED_RUNTIME_ENV:
    logger.info(
        "Hydrated %d persisted runtime environment variable(s)",
        len(_HYDRATED_RUNTIME_ENV),
    )


_INSTANCE_LOCK_PATH = Path(__file__).parent.parent / "artifacts" / "bot-api.lock"
_ACTIVE_SHUTDOWN = None


def _record_backup_validation_status(validation, latency_ms: int) -> None:
    """Publish the startup model-list probe to the shared LLM health tracker.

    The public health endpoint is intentionally passive. Without this handoff, a
    freshly restarted but healthy service reports ``llm_unavailable`` until the
    first user-triggered completion. The startup validation already performed a
    credentialed endpoint/model check, so its result is safe to reuse.
    """
    from src.llm import status_tracker

    if getattr(validation, "status", None) == "available":
        status_tracker.record_success("backup", max(0, int(latency_ms)))
        return
    reason = str(
        getattr(validation, "reason", None)
        or getattr(validation, "status", None)
        or "backup_validation_failed"
    )
    status_tracker.record_failure("backup", reason)


def main() -> None:
    """Run exactly one Bot/API instance for this workspace."""
    global _ACTIVE_SHUTDOWN

    from src.utils.single_instance import (
        InstanceAlreadyRunningError,
        ProcessInstanceLock,
    )

    try:
        instance_lock = ProcessInstanceLock.acquire(_INSTANCE_LOCK_PATH)
    except InstanceAlreadyRunningError:
        logger.error(
            "Bot/API startup refused because another instance owns %s",
            _INSTANCE_LOCK_PATH,
        )
        raise SystemExit(2)

    _ACTIVE_SHUTDOWN = None
    try:
        _run_main()
    finally:
        shutdown = _ACTIVE_SHUTDOWN
        _ACTIVE_SHUTDOWN = None
        if shutdown is not None:
            try:
                shutdown()
            finally:
                try:
                    atexit.unregister(shutdown)
                except Exception:
                    pass
        instance_lock.release()


def _run_main() -> None:
    from config.settings import CONFIG, inject_llm_keys, validate_config
    from src.db.connection import (
        check_integrity,
        close_all_connections,
        close_write_pool, scoped_write,
    )
    from src.db.schema import apply_schema
    from src.db.migrations import run_migrations
    from src.telegram.bot import create_bot

    # Validate essential config before proceeding
    config_errors = validate_config(include_load_error=True)
    if config_errors:
        for err in config_errors:
            logger.error("Config error: %s", err)
        logger.error("Fix config.json and restart. Exiting.")
        sys.exit(1)

    # Inject LLM API keys from config.json into environment
    inject_llm_keys(CONFIG)

    # Validate the optional backup route without blocking startup on endpoint outages.
    from src.llm.backup_availability import validate_configured_backup
    from src.llm.routing import get_backup_config
    backup_probe_started = time.monotonic()
    backup_validation = validate_configured_backup(get_backup_config())
    _record_backup_validation_status(
        backup_validation,
        round((time.monotonic() - backup_probe_started) * 1000),
    )
    if backup_validation.status == "unavailable":
        logger.warning(
            "Backup LLM disabled for this process reason=%s",
            backup_validation.reason,
        )
    elif backup_validation.status == "unknown":
        logger.warning(
            "Backup LLM validation inconclusive reason=%s; failover remains enabled",
            backup_validation.reason,
        )
    else:
        logger.info("Backup LLM model validation passed")

    from config.settings import apply_runtime_flags
    apply_runtime_flags(CONFIG)

    # Fail before DB or network startup if contracts, exposure, and dispatch drift.
    from src.tools.integrity import ToolIntegrityError, validate_tool_integrity
    try:
        validate_tool_integrity()
    except ToolIntegrityError as exc:
        logger.critical("%s", exc)
        sys.exit(1)

    # Init DB — use scoped_write for schema/migrations, then release
    with scoped_write() as conn:
        apply_schema(conn)
        run_migrations(conn)

    # Integrity check uses a fresh read connection
    import sqlite3
    from config.settings import DB_PATH
    _check_conn = sqlite3.connect(str(DB_PATH), timeout=10)
    _check_conn.row_factory = sqlite3.Row
    _check_conn.execute("PRAGMA journal_mode = WAL")
    if not check_integrity(_check_conn):
        logger.critical("DATABASE INTEGRITY CHECK FAILED — refusing to start")
        # Push error card at most once per 10 minutes to avoid spamming on restart loops
        _flag = Path(__file__).parent.parent / ".integrity_alert_sent"
        _should_alert = True
        if _flag.exists():
            try:
                age = time.time() - _flag.stat().st_mtime
                if age < 600:  # 10 minutes cooldown
                    _should_alert = False
            except OSError:
                pass
        if _should_alert:
            try:
                from src.telegram.card_push import push_error_card_sync
                push_error_card_sync(
                    _check_conn,
                    task="startup",
                    error_class="IntegrityError",
                    error_message="Database integrity check failed. Bot halted.",
                    trace_id="startup",
                )
                _flag.write_text(str(time.time()))
            except Exception:
                pass  # best-effort alert
        _check_conn.close()
        sys.exit(1)
    else:
        logger.info("Database ready at %s, integrity OK", CONFIG.database_file)
    _check_conn.close()

    # Reconcile API attachment filesystem decisions before any worker, bot, or
    # integrated API can read or send a partially mutated draft.
    from src.email.attachment_recovery import recover_pending
    with scoped_write() as conn:
        recovered = recover_pending(conn, CONFIG.data_dir)
    if recovered:
        logger.warning("Reconciled %d draft attachment operations", recovered)

    # bot reference — set after create_bot() so _shutdown can stop the scheduler
    _bot_ref = None
    _api_ref = None

    # Register idempotent cleanup for normal exit, SIGTERM, and failures.
    _shutdown_result: bool | None = None

    def _shutdown() -> bool:
        nonlocal _shutdown_result
        if _shutdown_result is not None:
            return _shutdown_result
        bot_stopped = True
        if _bot_ref is not None:
            try:
                _bot_ref.stop()
            except Exception:
                logger.exception("Telegram bot shutdown failed")
                bot_stopped = False
        # The integrated API borrows the bot's DB and workers. Drain requests
        # before closing the shared writer, even when the bot stop failed.
        api_drained = True
        if _api_ref is not None:
            try:
                api_drained = _stop_api_server(_api_ref)
            except Exception:
                logger.exception("Integrated API shutdown failed")
                api_drained = False
        workers_stopped = True
        from importlib import import_module
        for name, module_name, function_name, timeout in (
            ("session touch", "src.auth.session_touch", "stop_session_touch_worker", 2.0),
            ("tool shadow", "src.assistant.tool_centric_shadow_worker", "stop_tool_centric_shadow_worker", 5.0),
            ("email shadow", "src.email.project_truth.shadow_worker", "stop_email_project_truth_shadow_worker", 5.0),
            ("trace pipeline", "src.observability.pipeline", "stop_trace_pipeline", 5.0),
        ):
            try:
                stop = getattr(import_module(module_name), function_name)
                if stop(timeout=timeout) is not True:
                    logger.error("%s worker did not stop", name)
                    workers_stopped = False
            except Exception:
                logger.exception("%s worker shutdown failed", name)
                workers_stopped = False
        flushed = False
        try:
            from src.telemetry.setup import force_flush_telemetry
            flushed = force_flush_telemetry(timeout_millis=5000) is True
            if not flushed:
                logger.error("Telemetry flush did not complete")
        except Exception:
            logger.exception("Telemetry flush failed")
        if not (bot_stopped and api_drained and workers_stopped):
            # Never close shared DB resources under still-running producers.
            logger.error("Runtime did not drain; shared DB resources remain open")
            _shutdown_result = False
            return False
        db_closed = True
        try:
            close_write_pool()
        except Exception:
            logger.exception("Shared write pool close failed")
            db_closed = False
        # A best-effort checkpoint helper swallows busy errors. Inspect the
        # actual SQLite result here before acknowledging orderly shutdown.
        try:
            import sqlite3 as _sql
            from config.settings import DB_PATH as _dbp
            wal_conn = _sql.connect(str(_dbp), timeout=5)
            try:
                outcome = wal_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if outcome is None or outcome[0] != 0:
                    raise RuntimeError(f"WAL checkpoint incomplete: {outcome}")
            finally:
                wal_conn.close()
        except Exception:
            logger.exception("Shutdown WAL checkpoint failed")
            db_closed = False
        try:
            close_all_connections()
        except Exception:
            logger.exception("Shared DB connections close failed")
            db_closed = False
        _shutdown_result = flushed and db_closed
        return _shutdown_result

    global _ACTIVE_SHUTDOWN
    _ACTIVE_SHUTDOWN = _shutdown
    atexit.register(_shutdown)

    def _sigterm_handler(signum, frame):
        logger.info("Received SIGTERM, shutting down…")
        sys.exit(0)  # triggers atexit

    signal.signal(signal.SIGTERM, _sigterm_handler)

    # Enable LLM observability tracing
    from src.llm.client import enable_tracing
    enable_tracing()
    logger.info("LLM tracing enabled")

    # Enable OpenTelemetry distributed tracing
    from src.telemetry.setup import init_telemetry
    init_telemetry(service_name="agentfixbpro-bot")
    logger.info("OpenTelemetry tracing enabled")

    # Start background consumers before any bot/API producers can emit work.
    from src.auth.session_touch import start_session_touch_worker
    from src.assistant.tool_centric_shadow_worker import start_tool_centric_shadow_worker
    from src.email.project_truth.shadow_worker import (
        email_project_truth_mode,
        start_email_project_truth_shadow_worker,
    )
    from src.observability.pipeline import start_trace_pipeline
    start_trace_pipeline()
    start_session_touch_worker()
    start_tool_centric_shadow_worker()
    email_shadow_worker = start_email_project_truth_shadow_worker()
    logger.info(
        "Engineering trace/session workers started; email project truth mode=%s, shadow_worker_running=%s",
        email_project_truth_mode(),
        email_shadow_worker is not None,
    )

    # Create and start bot
    bot = create_bot()
    _bot_ref = bot
    logger.info("Starting Telegram bot (chat_id=%s)…", bot.chat_id)
    bot.start_in_thread()

    # Start API server in-process (same WriteSerializer = no cross-process DB lock)
    api_port = getattr(CONFIG, "api_port", 8000) or 8000
    from contextlib import nullcontext
    from scripts.service_shutdown import cooperative_shutdown_event
    # The main thread owns the event and the existing finally cleanup. The
    # guard waits for process exit, not merely for Uvicorn to release its port.
    stop_context = (
        cooperative_shutdown_event(_INSTANCE_LOCK_PATH.parent.parent)
        if sys.platform == "win32" else nullcontext(None)
    )
    with stop_context as stop_event:
        _api_ref = _start_api_server(api_port)
        logger.info("Bot + API running (port %d). Press Ctrl-C to stop.", api_port)
        try:
            while True:
                if stop_event is not None and stop_event.wait(1):
                    break
                if stop_event is None:
                    time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Ctrl-C received, shutting down")
        finally:
            cleanup_succeeded = _shutdown()
            if cleanup_succeeded and stop_event is not None:
                stop_event.confirm_cleanup()
        raise SystemExit(0 if cleanup_succeeded else 1)


def _stop_api_server(runtime: tuple[object, threading.Thread]) -> bool:
    """Drain integrated API requests before bot-owned workers and DB close."""
    server, thread = runtime
    server.should_exit = True
    thread.join(timeout=15.0)
    if thread.is_alive():
        logger.warning("Integrated API did not stop gracefully; requesting forced exit")
        server.force_exit = True
        thread.join(timeout=5.0)
        return False  # A forced Uvicorn exit cannot acknowledge graceful drain.
    return True


def _start_api_server(port: int) -> tuple[object, threading.Thread]:
    """Start uvicorn API server in a daemon thread."""
    import uvicorn
    from src.api.app import create_app

    app = create_app(runtime_mode="integrated")

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",  # suppress uvicorn access logs — API logging is separate
        access_log=False,
    )
    server = uvicorn.Server(config)

    # Override shutdown signal handling — our main thread handles signals
    server.install_signal_handlers = lambda: None

    def _run():
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(server.serve())
        finally:
            asyncio.set_event_loop(None)
            loop.close()

    t = threading.Thread(target=_run, daemon=True, name="api-server")
    t.start()
    logger.info("API server thread launched for port %d (in-process)", port)
    return server, t


if __name__ == "__main__":
    main()
