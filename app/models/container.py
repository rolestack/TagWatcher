import uuid
from datetime import datetime
from sqlalchemy import String, Boolean, DateTime, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base


class TrackedContainer(Base):
    __tablename__ = "tracked_containers"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    docker_host_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("docker_hosts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Docker's own container ID (short or full)
    container_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    # Full image reference, e.g. "nginx:latest" or "ghcr.io/owner/repo:v1.2.3"
    image: Mapped[str] = mapped_column(String(512), nullable=False)
    tag: Mapped[str] = mapped_column(String(256), nullable=False, default="latest")
    # Current running digest
    digest: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # Latest discovered version/digest from registry
    latest_tag: Mapped[str | None] = mapped_column(String(256), nullable=True)
    latest_digest: Mapped[str | None] = mapped_column(String(256), nullable=True)
    release_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    has_update: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Per-container version strategy override; None = inherit from host
    version_strategy_override: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Glob pattern used when version_strategy_override == "custom" (e.g. "29.2.*")
    version_pattern: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(64), nullable=False, default="running")
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # When set, suppress notifications until this timestamp (per-container snooze)
    snoozed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)

    # Relationships
    docker_host: Mapped["DockerHost"] = relationship("DockerHost", back_populates="tracked_containers")  # noqa: F821
    notification_logs: Mapped[list["NotificationLog"]] = relationship(  # noqa: F821
        "NotificationLog",
        back_populates="container",
        lazy="selectin",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<TrackedContainer id={self.id} name={self.name} image={self.image}:{self.tag}>"
