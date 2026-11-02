"""Import every model module here so Base.metadata is complete for Alembic."""

from app.models.ai import Conversation, Message, ModelInventory
from app.models.auth import AuditEvent, Role, User, UserSession

__all__ = [
    "AuditEvent",
    "Conversation",
    "Message",
    "ModelInventory",
    "Role",
    "User",
    "UserSession",
]
