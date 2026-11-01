from datetime import datetime

from sqlalchemy import DateTime
from sqlalchemy import Enum
from sqlalchemy import ForeignKey
from sqlalchemy import Index
from sqlalchemy import String

from sqlalchemy.dialects.postgresql import UUID

from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship

from app.database.base import Base
from app.enums.enquiry_category import EnquiryCategory
from app.enums.enquiry_status import EnquiryStatus
from app.mixins.timestamp import TimestampMixin

class Enquiry(TimestampMixin, Base):

    __tablename__ = "enquiries"

    __table_args__ = (
        Index("ix_enquiries_user_id_created_at", "user_id", "created_at"),
        Index("ix_enquiries_status", "status"),
        Index("ix_enquiries_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user_profiles.id"),
    )

    subject: Mapped[str] = mapped_column(
        String(200)
    )

    category: Mapped[EnquiryCategory] = mapped_column(
        Enum(EnquiryCategory),
        default=EnquiryCategory.GENERAL,
    )

    message: Mapped[str] = mapped_column(
        String(1000)
    )

    status: Mapped[EnquiryStatus] = mapped_column(
        Enum(EnquiryStatus),
        default=EnquiryStatus.OPEN,
    )

    admin_reply: Mapped[str | None] = mapped_column(
        String(1000), nullable=True
    )

    resolved_by: Mapped[str | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user_profiles.id"),
        nullable=True,
    )

    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user = relationship("UserProfile", foreign_keys=[user_id])
    resolver = relationship("UserProfile", foreign_keys=[resolved_by])
