from datetime import datetime

from sqlalchemy import DateTime
from sqlalchemy import Enum
from sqlalchemy import ForeignKey
from sqlalchemy import Float
from sqlalchemy import Index

from sqlalchemy.dialects.postgresql import UUID

from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship

from app.database.base import Base
from app.enums.journey_status import JourneyStatus
from app.mixins.timestamp import TimestampMixin

class Journey(TimestampMixin, Base):

    __tablename__ = "journeys"

    __table_args__ = (
        Index("ix_journeys_user_id_status", "user_id", "status"),
        Index("ix_journeys_status_checkin_time", "status", "checkin_time"),
        Index("ix_journeys_source_station_id_status", "source_station_id", "status"),
        Index("ix_journeys_destination_station_id_status", "destination_station_id", "status"),
    )

    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True
    )

    user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user_profiles.id"),
        nullable=False
    )

    source_station_id: Mapped[int] = mapped_column(
        ForeignKey("stations.id"),
        nullable=False
    )

    destination_station_id: Mapped[int] = mapped_column(
        ForeignKey("stations.id"),
        nullable=False
    )

    checkin_time: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False
    )

    checkout_time: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True
    )

    fare: Mapped[float] = mapped_column(
        Float,
        default=0
    )

    status: Mapped[JourneyStatus] = mapped_column(
        Enum(JourneyStatus),
        default=JourneyStatus.ACTIVE
    )

    user = relationship(
        "UserProfile",
        back_populates="journeys"
    )

    source_station = relationship(
        "Station",
        foreign_keys=[source_station_id],
        back_populates="journeys_from"
    )

    destination_station = relationship(
        "Station",
        foreign_keys=[destination_station_id],
        back_populates="journeys_to"
    )