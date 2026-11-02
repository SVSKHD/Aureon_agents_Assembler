"""Import every model module here so Base.metadata is complete for Alembic."""

from app.models import brokers, trading  # noqa: F401  (registers trading/dhan/mt5 tables)
from app.models.agents import Agent, AgentLog, AgentRun, ScheduledJob
from app.models.ai import Conversation, Message, ModelInventory
from app.models.approvals import ApprovalEvent, ApprovalRequest
from app.models.auth import AuditEvent, Role, User, UserSession
from app.models.finance import FinanceAttachment, FinanceIdentity, FinanceTombstone
from app.models.github import (
    GitHubConnection,
    GitHubIssueLink,
    GitHubRepository,
    GitHubSyncRun,
    GitHubTrackedOperation,
    GitHubWebhookEvent,
    GitHubWorkflowJob,
    GitHubWorkflowRun,
)
from app.models.imaging import GeneratedAsset, ImageJob
from app.models.repairs import Incident, RepairTask
from app.models.reviews import Review

__all__ = [
    "Agent",
    "AgentLog",
    "AgentRun",
    "ApprovalEvent",
    "ApprovalRequest",
    "AuditEvent",
    "Conversation",
    "FinanceAttachment",
    "FinanceIdentity",
    "FinanceTombstone",
    "GeneratedAsset",
    "GitHubConnection",
    "GitHubIssueLink",
    "GitHubRepository",
    "GitHubSyncRun",
    "GitHubTrackedOperation",
    "GitHubWebhookEvent",
    "GitHubWorkflowJob",
    "GitHubWorkflowRun",
    "ImageJob",
    "Incident",
    "Message",
    "ModelInventory",
    "RepairTask",
    "Review",
    "Role",
    "ScheduledJob",
    "User",
    "UserSession",
]
