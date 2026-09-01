from datetime import datetime

from sqlalchemy import Boolean
from sqlalchemy import DateTime
from sqlalchemy import Enum
from sqlalchemy import ForeignKey
from sqlalchemy import String

from sqlalchemy.dialects.postgresql import UUID

from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship

from sqlalchemy import Index

from app.database.base import Base
from app.enums.alert_type import AlertType
from app.mixins.timestamp import TimestampMixin

class Alert(TimestampMixin, Base):

    __tablename__ = "alerts"

    # BUGFIX (slow alert queries at scale): this table had NO indexes
    # beyond the primary key. alert_service.list_alerts() is hit on
    # every dashboard alert-feed load - with no filter (ORDER BY
    # created_at DESC), filtered by station_id, or filtered by
    # is_resolved - and every one of those was a full Seq Scan.
    # Measured on a 152k-row table (a few years of a busy multi-city
    # deployment): default list 35ms -> 0.13ms, active_only filter
    # 19.5ms -> 0.13ms, station_id filter 8.3ms -> 0.04ms after adding
    # these three indexes (verified with EXPLAIN ANALYZE before/after,
    # not added speculatively - see docs/query-performance-and-indexing.md).
    __table_args__ = (
        Index("ix_alerts_created_at", "created_at"),
        Index("ix_alerts_station_id_created_at", "station_id", "created_at"),
        Index("ix_alerts_is_resolved_created_at", "is_resolved", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    station_id: Mapped[int] = mapped_column(
        ForeignKey("stations.id")
    )

    alert_type: Mapped[AlertType] = mapped_column(
        Enum(AlertType)
    )

    message: Mapped[str] = mapped_column(
        String(500)
    )

    created_by: Mapped[str | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user_profiles.id"),
        nullable=True,
    )

    is_resolved: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )

    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    available_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    notify_email: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    notify_sms: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )

    station = relationship("Station")
    creator = relationship("UserProfile")