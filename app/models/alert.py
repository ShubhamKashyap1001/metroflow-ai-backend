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

from app.database.base import Base
from app.enums.alert_type import AlertType
from app.mixins.timestamp import TimestampMixin

class Alert(TimestampMixin, Base):

    __tablename__ = "alerts"

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