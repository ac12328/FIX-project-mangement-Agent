"""Public package for durable project reconciliation from historical emails."""

from src.project_reconciliation.contracts import ReconciliationMode, ReconciliationResult
from src.project_reconciliation.service import reconcile_project_from_emails
from src.project_reconciliation.surface import (
    ReconciliationSurfaceExecution,
    ReconciliationSurfaceRequest,
    classify_reconciliation_request,
    execute_reconciliation_request,
    format_reconciliation_result,
)

__all__ = [
    "ReconciliationMode",
    "ReconciliationResult",
    "ReconciliationSurfaceExecution",
    "ReconciliationSurfaceRequest",
    "classify_reconciliation_request",
    "execute_reconciliation_request",
    "format_reconciliation_result",
    "reconcile_project_from_emails",
]
