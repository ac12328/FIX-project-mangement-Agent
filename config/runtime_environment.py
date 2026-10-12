"""Demo edition: never read credentials from the Windows user/machine profile."""
from __future__ import annotations
from collections.abc import Mapping, MutableMapping
from typing import Callable

PERSISTED_RUNTIME_ENV_NAMES: frozenset[str] = frozenset()
EnvironmentReader = Callable[[], tuple[Mapping[str, str], Mapping[str, str]]]


def hydrate_persisted_runtime_environment(
    *, environ: MutableMapping[str, str] | None = None,
    reader: EnvironmentReader | None = None,
) -> tuple[str, ...]:
    """Do not invoke a reader or inherit persisted production secrets."""
    return ()
