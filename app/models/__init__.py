"""Import every model module here so Base.metadata is complete for Alembic."""

from app.models import brokers, trading  # noqa: F401  (registers trading/dhan/mt5 tables)
from app.models.agents import Agent, AgentLog, AgentRun, ScheduledJob
from app.models.ai import Conversation, Message, ModelInventory
from app.models.app_settings import AppSetting
from app.models.approvals import (
    ApprovalArtifact,
    ApprovalEvent,
    ApprovalPolicy,
    ApprovalRequest,
)
from app.models.auth import AuditEvent, Role, User, UserSession
from app.models.devops import (
    Deployment,
    DeploymentStage,
    DevopsEvent,
    NotificationPrefs,
    NotificationRead,
    RepoEvent,
    Service,
    ServiceSnapshot,
)
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
from app.models.model_calls import ModelCall
from app.models.repairs import Incident, RepairTask
from app.models.reviews import Review

__all__ = [
    "Agent",
    "AgentLog",
    "AgentRun",
    "AppSetting",
    "ApprovalArtifact",
    "ApprovalEvent",
    "ApprovalPolicy",
    "ApprovalRequest",
    "AuditEvent",
    "Conversation",
    "Deployment",
    "DeploymentStage",
    "DevopsEvent",
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
    "ModelCall",
    "ModelInventory",
    "NotificationPrefs",
    "NotificationRead",
    "RepairTask",
    "RepoEvent",
    "Review",
    "Role",
    "ScheduledJob",
    "Service",
    "ServiceSnapshot",
    "User",
    "UserSession",
]
