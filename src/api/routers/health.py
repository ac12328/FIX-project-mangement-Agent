"""Health check endpoint — liveness probe for supervisor and monitoring."""

import logging
import sqlite3
import time
from fastapi import APIRouter, Depends
from src.api.deps import get_conn
from src.api.schemas.health import (
    HealthResponse,
    LLMHealthResponse,
    LLMProviderStatus,
    ToolStatusResponse,
)
from config.defaults import CURRENT_SCHEMA_VERSION

router = APIRouter(tags=["health"])
logger = logging.getLogger(__name__)

_start_time = time.time()

_STALE_SECONDS = 120


@router.get("/health")
def health_check(conn: sqlite3.Connection = Depends(get_conn)):
    from config.settings import CONFIG
    if CONFIG.dashboard_auth.production:
        try:
            conn.execute("SELECT 1")
            return {"status": "ok"}
        except Exception:
            return {"status": "degraded"}

    db_ok = True
    active_projects = 0
    last_heartbeat = None

    try:
        conn.execute("SELECT 1")
        row = conn.execute(
            "SELECT COUNT(*) FROM projects WHERE project_status = 'Active'"
        ).fetchone()
        active_projects = row[0] if row else 0

        hb = conn.execute(
            "SELECT last_telegram_interaction_at FROM operator_state WHERE id = 1"
        ).fetchone()
        if hb:
            last_heartbeat = hb[0]
    except Exception:
        db_ok = False

    from src.llm.status_tracker import get_status
    status_map = get_status()
    llm_ok = status_map["primary"].reachable or status_map["backup"].reachable

    if not db_ok:
        status = "degraded"
    elif not llm_ok:
        status = "llm_unavailable"
    else:
        status = "ok"

    return HealthResponse(
        status=status,
        uptime_seconds=round(time.time() - _start_time, 1),
        db_ok=db_ok,
        llm_ok=llm_ok,
        schema_version=CURRENT_SCHEMA_VERSION,
        active_projects=active_projects,
        last_bot_heartbeat=last_heartbeat,
    )


@router.get("/llm-status", response_model=LLMHealthResponse)
def llm_status():
    """Return LLM provider status from passive call tracking (no probing)."""
    from src.llm.status_tracker import get_status
    from src.llm.routing import get_primary_endpoint, get_backup_endpoint

    status_map = get_status()
    _, _, p_model = get_primary_endpoint()
    _, _, b_model = get_backup_endpoint()

    now = time.time()
    p = status_map["primary"]
    b = status_map["backup"]

    p_reachable = p.reachable and (now - p.last_check < _STALE_SECONDS) if p.last_check else False
    b_reachable = b.reachable and (now - b.last_check < _STALE_SECONDS) if b.last_check else False

    if p_reachable:
        active = "primary"
    elif b_reachable:
        active = "backup"
    else:
        active = "none"

    # Friendly labels from live routing (primary = active provider, backup = local Gemma)
    primary_name = "Primary LLM"
    backup_name = "Local Gemma 4"
    try:
        from config.settings import CONFIG

        prov = (CONFIG.llm_keys.active_provider or "").strip()
        if prov == "ollama":
            primary_name = "Ollama (GLM)"
        elif prov in ("custom",) or CONFIG.llm_keys.local_llm_api_base:
            primary_name = "Local LiteLLM Proxy"
        elif prov:
            primary_name = prov
        b_model_short = (b_model or "").rsplit("/", 1)[-1]
        if b_model_short:
            backup_name = f"Local {b_model_short}"
    except Exception:
        pass

    return LLMHealthResponse(
        primary=LLMProviderStatus(
            name=primary_name,
            reachable=p_reachable,
            latency_ms=p.last_latency_ms,
            model=p_model,
        ),
        backup=LLMProviderStatus(
            name=backup_name,
            reachable=b_reachable,
            latency_ms=b.last_latency_ms,
            model=b_model,
        ),
        active_provider=active,
    )

@router.get("/tool-status", response_model=ToolStatusResponse)
def tool_status():
    """Return passive tool selection, outcome, and circuit health telemetry."""
    from src.tools.health_tracker import get_status
    from src.tools.registry import TOOL_BY_NAME

    return get_status(TOOL_BY_NAME)
