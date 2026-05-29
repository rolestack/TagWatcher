import uuid
from datetime import datetime
from enum import Enum as PyEnum
from sqlalchemy import String, Boolean, DateTime, ForeignKey, Text, Enum
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID, JSONB

from app.database import Base


class ChannelType(str, PyEnum):
    slack = "slack"
    discord = "discord"
    telegram = "telegram"
    zulip = "zulip"
    mattermost = "mattermost"
    teams = "teams"


class NotificationChannel(Base):
    __tablename__ = "notification_channels"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    space_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("spaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    channel_type: Mapped[ChannelType] = mapped_column(
        Enum(ChannelType, name="channeltype"), nullable=False
    )
    # JSON config: webhook_url, bot_token, chat_id, etc. depending on channel_type
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)

    # Relationships
    space: Mapped["Space"] = relationship("Space", back_populates="notification_channels")  # noqa: F821
    notification_logs: Mapped[list["NotificationLog"]] = relationship(
        "NotificationLog",
        back_populates="channel",
        lazy="selectin",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<NotificationChannel id={self.id} name={self.name} type={self.channel_type}>"


class NotificationLog(Base):
    __tablename__ = "notification_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    container_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tracked_containers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    channel_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("notification_channels.id", ondelete="CASCADE"), nullable=False, index=True
    )
    old_tag: Mapped[str | None] = mapped_column(String(256), nullable=True)
    old_digest: Mapped[str | None] = mapped_column(String(256), nullable=True)
    new_tag: Mapped[str | None] = mapped_column(String(256), nullable=True)
    new_digest: Mapped[str | None] = mapped_column(String(256), nullable=True)
    release_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(64), nullable=False, default="sent")  # sent / failed / ack
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    status_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    container: Mapped["TrackedContainer"] = relationship(  # noqa: F821
        "TrackedContainer", back_populates="notification_logs",
        lazy="selectin",
    )
    channel: Mapped["NotificationChannel"] = relationship(
        "NotificationChannel", back_populates="notification_logs",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return f"<NotificationLog id={self.id} container={self.container_id} status={self.status}>"
