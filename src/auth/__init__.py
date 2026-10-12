"""Dashboard authentication and authorization services."""

from src.auth.rbac import ROLE_PERMISSIONS, require_permission

__all__ = ["ROLE_PERMISSIONS", "require_permission"]
