"""Read memory-reform feature flags from config.defaults."""

from __future__ import annotations

from config import defaults


def operator_memory_enabled() -> bool:
    return bool(getattr(defaults, "OPERATOR_MEMORY_ENABLED", False))


def memory_guard_enabled() -> bool:
    return bool(getattr(defaults, "MEMORY_GUARD_ENABLED", False)) and operator_memory_enabled()


def thread_artifacts_enabled() -> bool:
    return bool(getattr(defaults, "THREAD_ARTIFACTS_ENABLED", False))


def memory_flush_to_store() -> bool:
    return bool(getattr(defaults, "MEMORY_FLUSH_TO_STORE", False))