from datetime import datetime

from sqlalchemy import DateTime
from sqlalchemy import Enum
from sqlalchemy import ForeignKey
from sqlalchemy import Float

from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship

from app.database.base import Base
from app.enums.journey_status import JourneyStatus
from app.mixins.timestamp import TimestampMixin


class Journey(TimestampMixin, Base):

    __tablename__ = "journeys"

    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True
    )

    user_id: Mapped[str] = mapped_column(
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