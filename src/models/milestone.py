"""Legacy milestone retrieval hints and advisory ball defaults.

These mappings support retrieval, candidate discovery, and contextual advice only.
They are not completion evidence, milestone acceptance policy, or write authority.
The operational stage-gate source of truth is loaded by
``src.milestone.contracts`` from ``milestone-contracts.json``.
"""

from src.models.enums import BallWith, Milestone

# Retrieval hints only — never sufficient to establish milestone completion.
MILESTONE_RETRIEVAL_HINTS: dict[Milestone, list[str]] = {
    Milestone.M1_NETWORK_SETUP: [
        "network", "connectivity", "firewall", "IP", "port", "VPN",
        "UAT network", "initial connectivity",
    ],
    Milestone.M2_UAT_SESSION_CONFIGURATION: [
        "session", "SenderCompID", "TargetCompID", "Demo Connectivity", "FIX session",
    ],
    Milestone.M3_UAT_ENVIRONMENT_ACCESS: [
        "UAT", "test environment", "access", "credentials", "login",
    ],
    Milestone.M4_APPLICATION_TESTING: [
        "Demo Provisioning", "hub", "counterparty", "setup", "configuration",
    ],
    Milestone.M5_CONFORMANCE_TEST_CASES: [
        "conformance", "test case", "test result", "pass", "fail", "certification",
    ],
    Milestone.M6_TEST_REPORT_AND_PRODUCTION_PREPARATION: [
        "test report", "Production Preparation", "config", "configuration", "report",
    ],
    Milestone.M7_LOG_REVIEW_AND_PRODUCTION_SESSION_DELIVERY: [
        "log review", "production", "session delivery", "go-live prep", "prod session",
        "PCT", "production connectivity test", "prod VPN", "prod firewall",
    ],
    Milestone.M8_GO_LIVE: [
        "go live", "go-live", "production", "live", "launch",
        "first trade", "production logon",
    ],
}

# Advisory expectation only — actual ball_with follows the next accountable actor.
MILESTONE_TYPICAL_BALL_WITH: dict[Milestone, BallWith] = {
    Milestone.M1_NETWORK_SETUP: BallWith.EXTERNAL,
    Milestone.M2_UAT_SESSION_CONFIGURATION: BallWith.INTERNAL,
    Milestone.M3_UAT_ENVIRONMENT_ACCESS: BallWith.INTERNAL,
    Milestone.M4_APPLICATION_TESTING: BallWith.INTERNAL,
    Milestone.M5_CONFORMANCE_TEST_CASES: BallWith.EXTERNAL,
    Milestone.M6_TEST_REPORT_AND_PRODUCTION_PREPARATION: BallWith.EXTERNAL,
    Milestone.M7_LOG_REVIEW_AND_PRODUCTION_SESSION_DELIVERY: BallWith.INTERNAL,
    Milestone.M8_GO_LIVE: BallWith.SHARED,
}

# Backward-compatible aliases. New code should use the explicit advisory names.
MILESTONE_EVIDENCE_KEYWORDS = MILESTONE_RETRIEVAL_HINTS
MILESTONE_ENTRY_BALL_WITH = MILESTONE_TYPICAL_BALL_WITH
