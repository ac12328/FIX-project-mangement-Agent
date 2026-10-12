"""Delivery helpers for Dashboard verification and password reset emails."""

from __future__ import annotations

import logging
from urllib.parse import urlencode

from config.settings import CONFIG
from src.auth.local import ActionDelivery, EMAIL_VERIFICATION, PASSWORD_RESET
from src.email.sending import EmailSender, email_config_dict

logger = logging.getLogger(__name__)


def send_auth_action_email(delivery: ActionDelivery) -> bool:
    if delivery.action_type == EMAIL_VERIFICATION:
        path = "/verify-email"
        subject = "Verify your AgentFIXBPRO Dashboard account"
        purpose = "verify your email address"
    elif delivery.action_type == PASSWORD_RESET:
        path = "/reset-password"
        subject = "Reset your AgentFIXBPRO Dashboard password"
        purpose = "reset your password"
    else:
        raise ValueError("Unsupported authentication email type")

    base_url = (CONFIG.dashboard_base_url or "http://127.0.0.1:3317").rstrip("/")
    action_url = f"{base_url}{path}?{urlencode({'token': delivery.token})}"
    body = (
        f"Hello {delivery.display_name},\n\n"
        f"Use the secure link below to {purpose}:\n\n"
        f"{action_url}\n\n"
        "This link can be used once and will expire automatically. "
        "If you did not request this action, you can ignore this email.\n"
    )

    config = email_config_dict(CONFIG)
    if CONFIG.email.backend == "msgraph":
        config.update(
            {
                "tenant_id": CONFIG.msgraph.tenant_id,
                "client_id": CONFIG.msgraph.client_id,
                "client_secret": CONFIG.msgraph.client_secret,
                "user_email": CONFIG.msgraph.user_email,
            }
        )
    if CONFIG.email.backend == "console":
        # Authentication links must not be written to application logs.
        logger.info("Authentication email accepted by console backend for %s", delivery.email)
        return True

    sender = EmailSender(backend=CONFIG.email.backend, config=config)
    result = sender.send(delivery.email, subject, body)
    if not result.success:
        logger.error(
            "Authentication email delivery failed for %s via %s: %s",
            delivery.email,
            CONFIG.email.backend,
            result.error or "unknown error",
        )
    return bool(result.success)
