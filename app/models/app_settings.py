"""core.app_settings: runtime settings and secrets edited from Spasta (docs/settings.md).

Every value is stored encrypted (Fernet, SETTINGS_MASTER_KEY), secret or not, so the table
never holds a plaintext credential and one code path reads all of it. Only the keys in
app/services/app_settings.py REGISTRY can be written; bootstrap keys never live here.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AppSetting(Base):
    __tablename__ = "app_settings"
    __table_args__ = ({"schema": "core"},)

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    is_secret: Mapped[bool] = mapped_column(Boolean, nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("core.users.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"), nullable=False)
