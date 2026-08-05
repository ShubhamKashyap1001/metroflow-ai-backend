from sqlalchemy import ForeignKey

from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship

from app.database.base import Base


class LineStation(Base):

    __tablename__ = "line_stations"

    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True
    )

    line_id: Mapped[int] = mapped_column(
        ForeignKey("metro_lines.id"),
        nullable=False
    )

    station_id: Mapped[int] = mapped_column(
        ForeignKey("stations.id"),
        nullable=False
    )

    station_order: Mapped[int] = mapped_column(
        nullable=False
    )

    distance_from_previous: Mapped[float] = mapped_column(
        default=0
    )

    line = relationship(
        "MetroLine",
        back_populates="stations"
    )

    station = relationship(
        "Station",
        back_populates="metro_lines"
    )