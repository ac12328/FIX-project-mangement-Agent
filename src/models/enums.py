"""
Enums for AgentFIXBPRO.
All canonical enum values used across the system.
"""

from enum import Enum


class BallWith(str, Enum):
    INTERNAL = "Internal"
    EXTERNAL = "External"
    SHARED = "Shared"
    UNKNOWN = "Unknown"


class ProjectStatus(str, Enum):
    ACTIVE = "Active"
    BLOCKED = "Blocked"
    WAITING = "Waiting"
    COMPLETED = "Completed"
    ARCHIVED = "Archived"
    CANCELLED = "Cancelled"

    @classmethod
    def terminal_statuses(cls) -> frozenset["ProjectStatus"]:
        return frozenset({cls.COMPLETED, cls.ARCHIVED, cls.CANCELLED})


class Milestone(str, Enum):
    M1_NETWORK_SETUP = "M1_NetworkSetup"
    M2_UAT_SESSION_CONFIGURATION = "M2_UATSessionConfiguration"
    M3_UAT_ENVIRONMENT_ACCESS = "M3_UATEnvironmentAccess"
    M4_APPLICATION_TESTING = "M4_ApplicationTesting"
    M5_CONFORMANCE_TEST_CASES = "M5_ConformanceTestCases"
    M6_TEST_REPORT_AND_PRODUCTION_PREPARATION = "M6_TestReportAndProductionPreparation"
    M7_LOG_REVIEW_AND_PRODUCTION_SESSION_DELIVERY = "M7_LogReviewAndProductionSessionDelivery"
    M8_GO_LIVE = "M8_GoLive"

    @property
    def index(self) -> int:
        """0-based index in the milestone sequence."""
        return list(Milestone).index(self)

    @property
    def display_name(self) -> str:
        """Human-readable display name."""
        return MILESTONE_DISPLAY_NAMES.get(self.value, self.value)


# Display names for operator-facing output
MILESTONE_DISPLAY_NAMES: dict[str, str] = {
    "M1_NetworkSetup": "M1 - Network Setup",
    "M2_UATSessionConfiguration": "M2 - UAT Session Configuration",
    "M3_UATEnvironmentAccess": "M3 - UAT Environment Access",
    "M4_ApplicationTesting": "M4 - Application Testing",
    "M5_ConformanceTestCases": "M5 - Conformance Test Cases",
    "M6_TestReportAndProductionPreparation": "M6 - Test Report & Production Preparation",
    "M7_LogReviewAndProductionSessionDelivery": "M7 - Log Review & Production Session Delivery",
    "M8_GoLive": "M8 - Go Live",
}


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class WriteDecisionOutcome(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    NEEDS_REVIEW = "needs_review"
    DEFERRED = "deferred"


class TaskType(str, Enum):
    """Business task taxonomy for write/draft/audit (not pure LLM route keys).

    Pure routing-only keys (e.g. wiki_curator) live in ``LlmRoute``
    (``src.models.tasks``), not here.
    """
    TRUTH_WRITE_REASONING = "truth_write_reasoning"
    REPLY_TO_CLIENT = "reply_to_client"
    DRAFT_FOLLOW_UP = "draft_follow_up"
    ANSWER_STATUS = "answer_status"
    INTAKE_TRIAGE = "intake_triage"
    INTERNAL_FOLLOW_UP = "internal_follow_up"
    MORNING_BRIEF = "morning_brief"
    AGGREGATE_QUERY = "aggregate_query"
    SUPPORT_REPLY = "support_reply"
    RESEARCH = "research"
    PORTFOLIO_SCAN = "portfolio_scan"


class EvidenceType(str, Enum):
    """Project evidence_store evidence types (not ticket evidence)."""
    EMAIL = "email"
    COMMITMENT = "commitment"
    FACT = "fact"
    ATTACHMENT = "attachment"
    MANUAL_NOTE = "manual_note"


class TicketEvidenceType(str, Enum):
    """Support ticket evidence types (ticket_evidence table CHECK)."""
    EMAIL = "email"
    NOTE = "note"
    LOG_SNIPPET = "log_snippet"
    SCREENSHOT = "screenshot"
    RESOLUTION = "resolution"


class DraftStatus(str, Enum):
    CREATED = "created"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    SENT = "sent"
    DISCARDED = "discarded"
    SEND_FAILED = "send_failed"


class FollowupStatus(str, Enum):
    OPEN = "open"
    COMPLETED = "completed"
    OVERDUE = "overdue"
    CANCELLED = "cancelled"


class DeferredStatus(str, Enum):
    WAITING = "waiting"
    TRIGGERED = "triggered"
    DISMISSED = "dismissed"
    EXPIRED = "expired"


class SessionStatus(str, Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class OrgType(str, Enum):
    CLIENT = "client"
    VENDOR = "vendor"
    REGULATOR = "regulator"
    INTERNAL = "internal"
    UNKNOWN = "unknown"


class ProjectPartyRole(str, Enum):
    """Role of an organisation on a project (party graph)."""
    CLIENT = "client"
    VENDOR = "vendor"
    REGULATOR = "regulator"
    INTERNAL = "internal"
    COUNTERPARTY = "counterparty"
    OTHER = "other"


class ContactProjectRole(str, Enum):
    """Role of a person on a project."""
    PM = "pm"
    DEVELOPER = "developer"
    OPS = "ops"
    CERT = "cert"
    COMPLIANCE = "compliance"
    OTHER = "other"


class ContactType(str, Enum):
    PERSON = "person"
    DEPARTMENT = "department"
    UNKNOWN = "unknown"
    CORRESPONDENT = "correspondent"
    INTERNAL = "internal"


class TicketCategory(str, Enum):
    FIX_SESSION = "fix_session"
    NETWORK = "network"
    TRADE = "trade"
    POST_TRADE = "post_trade"
    CERT = "cert"
    CONFIG = "config"
    ACCESS = "access"
    MONITORING = "monitoring"
    OUTREACH = "outreach"
    OTHER = "other"


class TicketSeverity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class TicketStatus(str, Enum):
    OPEN = "open"
    INVESTIGATING = "investigating"
    WAITING_EXTERNAL = "waiting_external"
    WAITING_INTERNAL = "waiting_internal"
    RESOLVED = "resolved"
    CLOSED = "closed"

    @classmethod
    def terminal_statuses(cls) -> frozenset["TicketStatus"]:
        return frozenset({cls.RESOLVED, cls.CLOSED})


class TicketSourceType(str, Enum):
    EMAIL = "email"
    TELEGRAM = "telegram"
    MANUAL = "manual"


class WorkItemType(str, Enum):
    PROJECT = "project"
    SUPPORT_TICKET = "support_ticket"
    OUTREACH = "outreach"
    STANDALONE = "standalone"
    TASK = "task"
    COMMITMENT = "commitment"
    WAITING_FOR = "waiting_for"
    REMINDER = "reminder"


class WorkItemStatus(str, Enum):
    OPEN = "open"
    RESOLVED = "resolved"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class WorkItemPriority(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ProjectTier(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class InternalActionStatus(str, Enum):
    """Status for internal_actions (team-owned tasks; not internal_followups)."""
    OPEN = "open"
    DONE = "done"
    CANCELLED = "cancelled"


class OwnerTeam(str, Enum):
    """Internal owner teams for internal_actions CHECK constraint."""
    IT_NETWORK = "IT Network"
    COMPLIANCE = "Compliance"
    PROVISIONING_SUPPORT = "Demo Provisioning Support"
    CONNECTIVITY_ACCEPTANCE_SUPPORT = "Demo Connectivity Acceptance Support"
    DEMO_IT_SECURITY = "Demo IT Security"
    DEMO_TRADING_OPS = "Demo Trading Ops"
    DEMO_POST_TRADE = "Demo Post-Trade"
