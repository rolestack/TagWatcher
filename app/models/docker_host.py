import uuid
from datetime import datetime, timezone, timedelta
from sqlalchemy import String, Boolean, DateTime, Text, Integer, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base


class DockerHost(Base):
    __tablename__ = "docker_hosts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    space_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("spaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    host_type: Mapped[str] = mapped_column(String(32), nullable=False, default="tcp")
    host_url: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    use_tls: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Agent-type fields
    agent_registration_token: Mapped[str | None] = mapped_column(String(128), nullable=True)
    agent_registration_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    agent_secret: Mapped[str | None] = mapped_column(String(128), nullable=True)
    agent_allowed_cidrs: Mapped[str | None] = mapped_column(Text, nullable=True)
    tls_ca: Mapped[str | None] = mapped_column(Text, nullable=True)
    tls_cert: Mapped[str | None] = mapped_column(Text, nullable=True)
    tls_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    auto_check_updates: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    check_interval_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    check_schedule_time: Mapped[str | None] = mapped_column(Text, nullable=True)
    version_strategy: Mapped[str] = mapped_column(String(32), nullable=False, default="auto")
    version_pattern: Mapped[str | None] = mapped_column(String(200), nullable=True)
    exclude_patterns: Mapped[str | None] = mapped_column(Text, nullable=True)
    notification_snooze_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=24)
    # Runtime metadata (auto-detected by agent: "docker" or "kubernetes")
    runtime_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    runtime_metadata: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    agent_sync_interval: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_update_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    # Relationships
    space: Mapped["Space"] = relationship("Space", back_populates="docker_hosts")  # noqa: F821
    tracked_containers: Mapped[list["TrackedContainer"]] = relationship(  # noqa: F821
        "TrackedContainer",
        back_populates="docker_host",
        lazy="selectin",
        cascade="all, delete-orphan",
    )

    @property
    def is_agent_online(self) -> bool:
        """True if the agent has synced within 3× its reported sync interval (default 5 min)."""
        if not self.last_synced_at:
            return False
        interval = self.agent_sync_interval or 60
        threshold = datetime.now(timezone.utc) - timedelta(seconds=interval * 3)
        synced_at = self.last_synced_at
        if synced_at.tzinfo is None:
            synced_at = synced_at.replace(tzinfo=timezone.utc)
        return synced_at >= threshold

    def __repr__(self) -> str:
        return f"<DockerHost id={self.id} name={self.name} url={self.host_url}>"
