"""Critical Workspace reconciliation callbacks for a caller-owned commit."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass
class WorkspaceReceiptHooks:
    """Keep receipt verification separate from best-effort publication effects."""

    operation_id: str | None = None
    verify_receipt: Callable[[], None] | None = None
    refresh_response: Callable[[], None] | None = None
