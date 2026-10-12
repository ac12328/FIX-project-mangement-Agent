"""Process supervisor — manages bot, API, and UI processes with auto-restart."""

import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent


@dataclass
class ProcessConfig:
    name: str
    command: list[str]
    health_url: str | None = None
    cwd: str | None = None
    max_crashes: int = 5
    crash_window_seconds: int = 600
    startup_delay: float = 0


@dataclass
class SupervisorState:
    process: subprocess.Popen | None = None
    crash_times: list[float] = field(default_factory=list)
    total_restarts: int = 0


def should_restart(state: SupervisorState, *, max_crashes: int, window_seconds: int) -> bool:
    """Check if process should be restarted based on crash frequency."""
    now = time.time()
    recent = [t for t in state.crash_times if now - t < window_seconds]
    state.crash_times = recent  # prune old
    return len(recent) < max_crashes


def _start_process(config: ProcessConfig) -> subprocess.Popen:
    """Start a child process."""
    cwd = config.cwd or str(PROJECT_ROOT)
    logger.info("Starting %s: %s (cwd=%s)", config.name, " ".join(config.command), cwd)
    proc = subprocess.Popen(
        config.command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return proc


PROCESS_CONFIGS = [
    ProcessConfig(
        name="bot",
        command=[sys.executable, "scripts/run_bot.py"],
        startup_delay=3,
    ),
    ProcessConfig(
        name="api",
        command=[sys.executable, "-m", "uvicorn", "src.api.app:app",
                 "--host", "127.0.0.1", "--port", "8317"],
        health_url="http://127.0.0.1:8317/api/health",
        startup_delay=1,
    ),
    ProcessConfig(
        name="ui",
        command=["npm", "run", "dev"],
        cwd=str(PROJECT_ROOT / "dashboard"),
        startup_delay=0,
    ),
]


def run_supervisor():
    """Main supervisor loop; blocked in the preparation edition."""
    raise RuntimeError("Public preparation edition: service startup is blocked. See docs/PUBLIC_DEMO_POLICY.md")
    from src.utils.log_setup import configure_logging
    configure_logging("supervisor")

    states: dict[str, SupervisorState] = {}
    running = True

    def _shutdown(signum, frame):
        nonlocal running
        running = False
        logger.info("Supervisor shutting down (signal %s)", signum)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # Initial startup
    for config in PROCESS_CONFIGS:
        state = SupervisorState()
        state.process = _start_process(config)
        states[config.name] = state
        if config.startup_delay:
            time.sleep(config.startup_delay)

    logger.info("All services started — entering monitoring loop")

    try:
        _push_startup_alert()
    except Exception:
        logger.exception("Failed to send startup alert")

    # Monitor loop
    while running:
        time.sleep(10)

        for config in PROCESS_CONFIGS:
            state = states[config.name]
            if state.process is None:
                continue

            poll = state.process.poll()
            if poll is not None:
                # Process exited
                logger.warning(
                    "%s exited with code %d", config.name, poll,
                )
                state.crash_times.append(time.time())

                if should_restart(state, max_crashes=config.max_crashes,
                                  window_seconds=config.crash_window_seconds):
                    state.total_restarts += 1
                    logger.info(
                        "Restarting %s (attempt %d)", config.name, state.total_restarts,
                    )
                    try:
                        _push_restart_alert(config.name, state.total_restarts, config.max_crashes)
                    except Exception:
                        pass
                    state.process = _start_process(config)
                else:
                    logger.critical(
                        "%s crashed %d times in %ds — circuit breaker tripped",
                        config.name, config.max_crashes, config.crash_window_seconds,
                    )
                    try:
                        _push_circuit_breaker_alert(config.name)
                    except Exception:
                        pass
                    state.process = None

    # Graceful shutdown
    for config in PROCESS_CONFIGS:
        state = states[config.name]
        if state.process and state.process.poll() is None:
            logger.info("Stopping %s (pid %d)", config.name, state.process.pid)
            state.process.terminate()
            try:
                state.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                state.process.kill()


def _push_startup_alert():
    """Best-effort Telegram alert on startup."""
    text = "[Supervisor] All services started."
    if not _try_telegram(text):
        _send_email_alert("Supervisor startup", text)


def _push_restart_alert(name: str, attempt: int, max_attempts: int):
    text = f"[Supervisor] {name} crashed and was restarted (attempt {attempt}/{max_attempts})"
    if not _try_telegram(text):
        _send_email_alert(f"Supervisor restart: {name}", text)


def _push_circuit_breaker_alert(name: str):
    text = f"[EMERGENCY] {name} crashed repeatedly — circuit breaker tripped. Manual intervention needed."
    # Always try both channels for circuit breaker — this is critical
    tg_ok = _try_telegram(text)
    _send_email_alert(f"EMERGENCY: {name} circuit breaker", text)
    if not tg_ok:
        logger.critical("Telegram unreachable during circuit breaker alert — email fallback sent")


def _try_telegram(text: str) -> bool:
    """Attempt to send a Telegram message. Returns True on success."""
    try:
        import requests
        from config.settings import CONFIG
        bot_token = CONFIG.telegram.bot_token
        chat_id = CONFIG.telegram.chat_id
        resp = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
        return resp.ok
    except Exception:
        logger.debug("Telegram alert failed", exc_info=True)
        return False


def _send_email_alert(subject: str, body: str):
    """Best-effort email alert as backup channel when Telegram is unreachable."""
    try:
        import smtplib
        from email.mime.text import MIMEText
        from config.settings import CONFIG

        cfg = CONFIG.email
        if not cfg.smtp_host or not cfg.from_email:
            logger.debug("Email fallback skipped — SMTP not configured")
            return

        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = f"AgentFIXBPRO: {subject}"
        msg["From"] = cfg.from_email
        msg["To"] = cfg.from_email  # Alert goes to operator's own inbox

        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=15) as smtp:
            if cfg.smtp_tls:
                smtp.starttls()
            if cfg.smtp_username:
                smtp.login(cfg.smtp_username, cfg.smtp_password)
            smtp.send_message(msg)
        logger.info("Email fallback alert sent: %s", subject)
    except Exception:
        logger.exception("Email fallback alert also failed: %s", subject)


if __name__ == "__main__":
    run_supervisor()
