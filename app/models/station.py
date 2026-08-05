from sqlalchemy import Boolean
from sqlalchemy import Float
from sqlalchemy import String

from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship

from app.database.base import Base
from app.mixins.timestamp import TimestampMixin


class Station(TimestampMixin, Base):

    __tablename__ = "stations"

    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True
    )

    station_code: Mapped[str] = mapped_column(
        String(20),
        unique=True,
        nullable=False
    )

    station_name: Mapped[str] = mapped_column(
        String(120),
        nullable=False
    )

    city: Mapped[str] = mapped_column(
        String(100),
        nullable=False
    )

    latitude: Mapped[float] = mapped_column(
        Float,
        nullable=False
    )

    longitude: Mapped[float] = mapped_column(
        Float,
        nullable=False
    )

    is_interchange: Mapped[bool] = mapped_column(
        Boolean,
        default=False
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True
    )

    metro_lines = relationship(
        "LineStation",
        back_populates="station"
    )

    journeys_from = relationship(
        "Journey",
        foreign_keys="Journey.source_station_id",
        back_populates="source_station"
    )

    journeys_to = relationship(
        "Journey",
        foreign_keys="Journey.destination_station_id",
        back_populates="destination_station"
    )