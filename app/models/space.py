import uuid
from datetime import datetime
from sqlalchemy import String, DateTime
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base, utcnow
from app.models.group import group_spaces


class Space(Base):
    __tablename__ = "spaces"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    icon: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    # Relationships
    groups: Mapped[list["Group"]] = relationship(  # noqa: F821
        "Group",
        secondary=group_spaces,
        back_populates="spaces",
        lazy="selectin",
    )
    docker_hosts: Mapped[list["DockerHost"]] = relationship(  # noqa: F821
        "DockerHost",
        back_populates="space",
        lazy="selectin",
        cascade="all, delete-orphan",
    )
    notification_channels: Mapped[list["NotificationChannel"]] = relationship(  # noqa: F821
        "NotificationChannel",
        back_populates="space",
        lazy="selectin",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<Space id={self.id} name={self.name}>"
