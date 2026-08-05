import uuid

from sqlalchemy import Boolean
from sqlalchemy import Enum
from sqlalchemy import String

from sqlalchemy.dialects.postgresql import UUID

from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship

from app.database.base import Base
from app.enums.user_role import UserRole
from app.mixins.timestamp import TimestampMixin


class UserProfile(TimestampMixin, Base):

    __tablename__ = "user_profiles"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True
    )

    full_name: Mapped[str] = mapped_column(
        String(120),
        nullable=False
    )

    phone: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True
    )

    avatar_url: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True
    )

    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole),
        default=UserRole.PASSENGER
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True
    )

    journeys = relationship(
        "Journey",
        back_populates="user"
    )