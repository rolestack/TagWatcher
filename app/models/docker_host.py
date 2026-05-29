import uuid
from datetime import datetime
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
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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

    def __repr__(self) -> str:
        return f"<DockerHost id={self.id} name={self.name} url={self.host_url}>"
