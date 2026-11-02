"""Import every model module here so Base.metadata is complete for Alembic."""

from app.models import brokers, trading  # noqa: F401  (registers trading/dhan/mt5 tables)
from app.models.agents import Agent, AgentLog, AgentRun, ScheduledJob
from app.models.ai import Conversation, Message, ModelInventory
from app.models.auth import AuditEvent, Role, User, UserSession
from app.models.finance import FinanceIdentity
from app.models.imaging import GeneratedAsset, ImageJob

__all__ = [
    "Agent",
    "AgentLog",
    "AgentRun",
    "AuditEvent",
    "Conversation",
    "FinanceIdentity",
    "GeneratedAsset",
    "ImageJob",
    "Message",
    "ModelInventory",
    "Role",
    "ScheduledJob",
    "User",
    "UserSession",
]
