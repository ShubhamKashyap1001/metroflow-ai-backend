"""Passenger Enquiry module.

A passenger raises an enquiry (a support question / complaint /
lost-and-found report / suggestion, etc.). It starts life as OPEN.
An admin or operator can pick it up (IN_PROGRESS) and, once handled,
resolve it (RESOLVED) with a reply the passenger can read back on
their "My Enquiries" list - mirroring the Alert module's
raise -> resolve workflow, but scoped per-user instead of broadcast.
"""
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

    # BUGFIX (expensive analytics/user/enquiry queries): this table had
    # NO indexes at all beyond the primary key. enquiry_service.py's
    # list_enquiries() is hit on every "My Enquiries" page load (filtered
    # by user_id, ordered by created_at) and every admin "manage
    # enquiries" queue load (no filter, ordered by created_at), and
    # get_enquiry_stats() groups by status for the same user_id filter -
    # every one of those was a full Seq Scan of the whole enquiries
    # table, getting linearly slower as more enquiries pile up.
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
