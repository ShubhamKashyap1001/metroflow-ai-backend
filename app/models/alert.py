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

    acknowledged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    acknowledged_by_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user_profiles.id"), nullable=True
    )
    assigned_to_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user_profiles.id"), nullable=True
    )

    station = relationship("Station")
    creator = relationship("UserProfile", foreign_keys=[created_by])
    acknowledged_by = relationship("UserProfile", foreign_keys=[acknowledged_by_id])
    assigned_to = relationship("UserProfile", foreign_keys=[assigned_to_id])

    @property
    def acknowledged_by_name(self) -> str | None:
        return self.acknowledged_by.full_name if self.acknowledged_by else None

    @property
    def assigned_to_name(self) -> str | None:
        return self.assigned_to.full_name if self.assigned_to else None
