"""
Advisory knowledge base — static domain knowledge for project and support advisory.

Milestone PM guidance lives in wiki playbooks (see milestone_wiki.py).
This module holds event→advice mappings and support-category blueprints.
"""

# ---------------------------------------------------------------------------
# Project advisory: event → advice mapping per milestone
# ---------------------------------------------------------------------------

MILESTONE_ADVISORY: dict[str, dict] = {
    "M1_NetworkSetup": {
        "events": {
            "firewall_opened": {
                "assessment": "Network connectivity confirmed — ready to proceed.",
                "next_action": "Send session parameters (SenderCompID, TargetCompID, FIX version)",
                "priority": "now",
                "can_auto_draft": True,
            },
            "ip_confirmed": {
                "assessment": "IP addresses confirmed by counterparty.",
                "next_action": "Request internal firewall rule changes",
                "priority": "now",
            },
            "connectivity_test_passed": {
                "assessment": "Network test successful — milestone can advance.",
                "next_action": "Confirm milestone completion and move to M2",
                "priority": "now",
            },
            "stalled": {
                "assessment": "Network setup stalled — counterparty may need a nudge.",
                "next_action": "See wiki: pm-problem-index M1 stall policy",
                "wiki_resolve": True,
                "priority": "now",
                "can_auto_draft": True,
            },
        },
    },
    "M2_UATSessionConfiguration": {
        "events": {
            "session_params_sent": {
                "assessment": "Session parameters sent — awaiting counterparty confirmation.",
                "next_action": "See wiki: M2 session parameter follow-up",
                "wiki_resolve": True,
                "priority": "soon",
            },
            "params_confirmed": {
                "assessment": "Session parameters agreed — ready for UAT.",
                "next_action": "See wiki: M3 UAT readiness",
                "wiki_resolve": True,
                "priority": "now",
            },
        },
    },
    "M3_UATEnvironmentAccess": {
        "events": {
            "uat_login_confirmed": {
                "assessment": "Client can log in to UAT — ask them to send a test order.",
                "next_action": "See wiki: M3 UAT test order verification",
                "wiki_resolve": True,
                "priority": "now",
                "can_auto_draft": True,
            },
            "credentials_issued": {
                "assessment": "UAT credentials issued to counterparty.",
                "next_action": "See wiki: M3 UAT connectivity check",
                "wiki_resolve": True,
                "priority": "soon",
                "can_auto_draft": True,
            },
        },
    },
    "M4_ApplicationTesting": {
        "events": {
            "setup_submitted": {
                "assessment": "Setup request submitted — waiting for internal processing.",
                "next_action": "See wiki: M4 Demo Gateway A setup queue",
                "wiki_resolve": True,
                "priority": "soon",
            },
            "setup_complete": {
                "assessment": "Demo Gateway A setup complete — ready for conformance testing.",
                "next_action": "See wiki: M5 conformance kickoff",
                "wiki_resolve": True,
                "priority": "now",
                "can_auto_draft": True,
            },
        },
    },
    "M5_ConformanceTestCases": {
        "events": {
            "tests_started": {
                "assessment": "Counterparty has begun conformance testing.",
                "next_action": "See wiki: M5 conformance monitoring",
                "wiki_resolve": True,
                "priority": "soon",
            },
            "tests_passed": {
                "assessment": "All conformance tests passed — ready for test report.",
                "next_action": "See wiki: M6 test report request",
                "wiki_resolve": True,
                "priority": "now",
                "can_auto_draft": True,
            },
            "tests_failing": {
                "assessment": "Conformance tests failing — counterparty needs assistance.",
                "next_action": "See wiki: conformance troubleshooting playbook",
                "wiki_resolve": True,
                "priority": "now",
                "escalation_team": "IT-FIX",
            },
        },
    },
    "M6_TestReportAndProductionPreparation": {
        "events": {
            "report_received": {
                "assessment": "Test report received — needs review.",
                "next_action": "See wiki: M6 report review",
                "wiki_resolve": True,
                "priority": "now",
            },
            "cert_approved": {
                "assessment": "Certification approved — schedule PCT date.",
                "next_action": "See wiki: M7 PCT scheduling",
                "wiki_resolve": True,
                "priority": "now",
                "can_auto_draft": True,
            },
        },
    },
    "M7_LogReviewAndProductionSessionDelivery": {
        "events": {
            "log_review_complete": {
                "assessment": "Log review complete — prepare production session.",
                "next_action": "See wiki: M7 production session delivery",
                "wiki_resolve": True,
                "priority": "now",
                "can_auto_draft": True,
            },
            "production_params_sent": {
                "assessment": "Production parameters sent — awaiting counterparty setup.",
                "next_action": "See wiki: M8 production readiness follow-up",
                "wiki_resolve": True,
                "priority": "soon",
            },
        },
    },
    "M8_GoLive": {
        "events": {
            "go_live_date_set": {
                "assessment": "Go-live date confirmed — final preparations needed.",
                "next_action": "Verify production credentials and monitor first trades",
                "priority": "now",
            },
            "first_trade_success": {
                "assessment": "First production trade successful — project complete.",
                "next_action": "Send congratulations and close the project",
                "priority": "soon",
                "can_auto_draft": True,
            },
        },
    },
}


# ---------------------------------------------------------------------------
# Support advisory: per-category diagnostic/resolution blueprints
# ---------------------------------------------------------------------------

SUPPORT_ADVISORY: dict[str, dict] = {
    "fix_session": {
        "common_causes": [
            "Heartbeat interval mismatch",
            "Incorrect SenderCompID/TargetCompID",
            "Sequence number gap after restart",
            "Network timeout between endpoints",
            "SSL/TLS handshake failure",
        ],
        "diagnostic_steps": [
            "Check FIX engine logs for reject reason code",
            "Verify SenderCompID and TargetCompID match on both sides",
            "Compare heartbeat interval settings",
            "Check sequence numbers — may need reset",
            "Test network connectivity to counterparty endpoint",
        ],
        "resolution_actions": [
            "Reset sequence numbers (coordinate with counterparty)",
            "Update heartbeat interval to match",
            "Correct CompID configuration",
        ],
        "escalation_team": "IT-FIX",
        "typical_severity": "high",
    },
    "network": {
        "common_causes": [
            "Firewall rule expired or not applied",
            "VPN tunnel down",
            "DNS resolution failure",
            "Port blocked by intermediate firewall",
            "ISP routing issue",
        ],
        "diagnostic_steps": [
            "Ping/traceroute to counterparty endpoint",
            "Check firewall rules are active",
            "Verify VPN tunnel status",
            "Test TCP connectivity on specific port",
            "Check for recent network changes",
        ],
        "resolution_actions": [
            "Re-apply firewall rules",
            "Restart VPN tunnel",
            "Add IT-Network team to thread for investigation",
        ],
        "escalation_team": "IT-Network",
        "typical_severity": "high",
    },
    "cert": {
        "common_causes": [
            "Certificate expired",
            "Certificate chain incomplete",
            "Wrong certificate installed",
            "CA not trusted by counterparty",
        ],
        "diagnostic_steps": [
            "Check certificate expiry date",
            "Verify certificate chain is complete",
            "Confirm certificate matches the endpoint hostname",
            "Check if CA is in counterparty's trust store",
        ],
        "resolution_actions": [
            "Renew certificate",
            "Install complete certificate chain",
            "Share updated CA certificate with counterparty",
        ],
        "escalation_team": "IT-Security",
        "typical_severity": "high",
    },
    "trade": {
        "common_causes": [
            "Invalid instrument symbol or exchange",
            "Order size exceeds limits",
            "Account not enabled for instrument",
            "Price outside allowed range",
            "Missing required FIX tags",
        ],
        "diagnostic_steps": [
            "Check execution report reject reason (Tag 58)",
            "Verify instrument symbology mapping",
            "Check account permissions and limits",
            "Review order parameters against exchange rules",
        ],
        "resolution_actions": [
            "Update instrument mapping",
            "Adjust account limits",
            "Fix missing/incorrect FIX tags",
        ],
        "escalation_team": "Trading-Desk",
        "typical_severity": "medium",
    },
    "post_trade": {
        "common_causes": [
            "Allocation quantity mismatch",
            "Settlement instruction missing",
            "Position reconciliation discrepancy",
            "Counterparty confirmation pending",
        ],
        "diagnostic_steps": [
            "Compare allocation totals with execution quantity",
            "Check settlement instructions are complete",
            "Run position reconciliation report",
            "Verify counterparty affirmation status",
        ],
        "resolution_actions": [
            "Resubmit allocations",
            "Send settlement instructions",
            "Contact counterparty for confirmation",
        ],
        "escalation_team": "Operations",
        "typical_severity": "medium",
    },
    "config": {
        "common_causes": [
            "Parameter mismatch between environments",
            "Configuration not propagated after change",
            "Incorrect tag mapping",
        ],
        "diagnostic_steps": [
            "Compare configuration between UAT and production",
            "Review recent configuration changes",
            "Verify tag mappings match specification",
        ],
        "resolution_actions": [
            "Update configuration parameters",
            "Propagate changes to all environments",
        ],
        "escalation_team": "IT-FIX",
        "typical_severity": "medium",
    },
    "access": {
        "common_causes": [
            "Credentials expired or revoked",
            "IP not in whitelist",
            "Account locked after failed attempts",
        ],
        "diagnostic_steps": [
            "Check credential validity",
            "Verify IP whitelist includes client address",
            "Check account lock status",
        ],
        "resolution_actions": [
            "Reset or reissue credentials",
            "Update IP whitelist",
            "Unlock account",
        ],
        "escalation_team": "IT-Security",
        "typical_severity": "medium",
    },
}


def get_milestone_advisory(milestone: str) -> dict | None:
    """Get advisory knowledge for a milestone. Returns None if unknown."""
    return MILESTONE_ADVISORY.get(milestone)


def get_support_advisory(category: str) -> dict | None:
    """Get support advisory knowledge for a ticket category."""
    return SUPPORT_ADVISORY.get(category)


def match_event_to_advisory(milestone: str, event_keywords: list[str]) -> dict | None:
    """Match event keywords to a specific milestone advisory event.

    Returns the best-matching event advisory dict, or None.
    """
    milestone_adv = MILESTONE_ADVISORY.get(milestone)
    if not milestone_adv:
        return None
    events = milestone_adv.get("events", {})
    if not events:
        return None

    # Simple keyword overlap scoring
    kw_lower = {k.lower() for k in event_keywords}
    best_score = 0
    best_event = None
    for event_key, event_data in events.items():
        event_words = set(event_key.lower().replace("_", " ").split())
        overlap = len(kw_lower & event_words)
        if overlap > best_score:
            best_score = overlap
            best_event = event_data
    if best_event and best_event.get("wiki_resolve"):
        from src.advisory.milestone_wiki import search_milestone_context
        wiki_text = search_milestone_context(milestone)
        if wiki_text:
            enriched = dict(best_event)
            excerpt = wiki_text.strip().replace("\n", " ")[:280]
            # P1-12: keep the curated static next_action (e.g. "See wiki: M3 UAT
            # test order verification") as the human-readable action; attach the
            # raw wiki excerpt as a separate field instead of overwriting
            # next_action with a raw `[wiki:...>...] (score=1.0) ### ...` blob
            # the PM cannot read as an action.
            if excerpt:
                enriched["wiki_excerpt"] = excerpt
            return enriched
    return best_event
