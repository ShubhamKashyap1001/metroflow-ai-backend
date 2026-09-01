"""Mirrors Supabase's `auth.users` table 1:1 by id. Supabase owns the
actual credentials (email + password hash) in its own `auth` schema -
we never store a password here. This table only exists to attach
app-specific fields (role, phone, avatar) to a Supabase user id.

Rows are created lazily on first authenticated request - see
`app/core/security.py::_get_or_create_profile`.
"""
from sqlalchemy import Boolean
from sqlalchemy import Enum
from sqlalchemy import Index
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

    # BUGFIX (expensive analytics/user/enquiry queries): the admin
    # "User Management" list (app/api/v1/users.py::get_users) is now
    # ordered by created_at (paginated, see DEFAULT_USERS_LIMIT/
    # MAX_USERS_LIMIT there) - without this index that ORDER BY +
    # LIMIT/OFFSET still requires a full sort of every row in
    # user_profiles on every page. `email` already gets an index for
    # free from its `unique=True` constraint below.
    __table_args__ = (
        Index("ix_user_profiles_created_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True
    )

    email: Mapped[str | None] = mapped_column(
        String(255),
        unique=True,
        nullable=True
    )

    full_name: Mapped[str] = mapped_column(
        String(120),
        nullable=False
    )

    username: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True
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
