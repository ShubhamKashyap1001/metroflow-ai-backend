"""News & Announcements module.

Admin/operator-authored notices ("latest news") shown to every
passenger on the Enquiry & News page - service updates, general
announcements, etc. Deliberately separate from the Alert module
(app/models/alert.py), which is station-scoped and time-critical
(overcrowding/delay/emergency) with email/SMS dispatch. News items are
lighter-weight, not tied to a station, and not dispatched by
email/SMS - just published/unpublished.
"""
from sqlalchemy import Boolean
from sqlalchemy import ForeignKey
from sqlalchemy import String

from sqlalchemy.dialects.postgresql import UUID

from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship

from app.database.base import Base
from app.mixins.timestamp import TimestampMixin

class News(TimestampMixin, Base):

    __tablename__ = "news"

    id: Mapped[int] = mapped_column(primary_key=True)

    title: Mapped[str] = mapped_column(
        String(200)
    )

    content: Mapped[str] = mapped_column(
        String(2000)
    )

    created_by: Mapped[str | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user_profiles.id"),
        nullable=True,
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )

    author = relationship("UserProfile")
