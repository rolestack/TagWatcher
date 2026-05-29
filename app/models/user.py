import uuid
from datetime import datetime
from enum import Enum as PyEnum
from sqlalchemy import String, Boolean, DateTime, Table, Column, ForeignKey, Text, Enum
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base

user_groups = Table(
    "user_groups",
    Base.metadata,
    Column("user_id", UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("group_id", UUID(as_uuid=True), ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True),
)


class AuthType(str, PyEnum):
    local = "local"
    oidc = "oidc"


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Local auth fields
    username: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True, index=True)
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    auth_type: Mapped[str] = mapped_column(
        Enum(AuthType, name="auth_type_enum"),
        nullable=False,
        default=AuthType.oidc,
    )

    # OIDC fields
    oidc_sub: Mapped[str | None] = mapped_column(String(512), unique=True, nullable=True, index=True)

    # Common profile
    email: Mapped[str | None] = mapped_column(String(320), unique=True, nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    picture: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    groups: Mapped[list["Group"]] = relationship(  # noqa: F821
        "Group",
        secondary=user_groups,
        back_populates="users",
        lazy="selectin",
    )

    @property
    def display_name(self) -> str:
        return self.name or self.username or self.email or "Unknown"

    @property
    def display_email(self) -> str:
        return self.email or f"{self.username}@local" if self.username else "—"

    def __repr__(self) -> str:
        return f"<User id={self.id} auth={self.auth_type} name={self.display_name}>"
