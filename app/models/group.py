import uuid
from datetime import datetime
from sqlalchemy import String, DateTime, Table, Column, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base
from app.models.user import user_groups

# Association table for Group <-> Space many-to-many
group_spaces = Table(
    "group_spaces",
    Base.metadata,
    Column("group_id", UUID(as_uuid=True), ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True),
    Column("space_id", UUID(as_uuid=True), ForeignKey("spaces.id", ondelete="CASCADE"), primary_key=True),
)


class Group(Base):
    __tablename__ = "groups"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(256), unique=True, nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)

    # Relationships
    users: Mapped[list["User"]] = relationship(  # noqa: F821
        "User",
        secondary=user_groups,
        back_populates="groups",
        lazy="selectin",
    )
    spaces: Mapped[list["Space"]] = relationship(  # noqa: F821
        "Space",
        secondary=group_spaces,
        back_populates="groups",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return f"<Group id={self.id} name={self.name}>"
