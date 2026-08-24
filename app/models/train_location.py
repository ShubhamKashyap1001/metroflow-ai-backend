from sqlalchemy import Float
from sqlalchemy import ForeignKey
from sqlalchemy import String
from sqlalchemy import UniqueConstraint

from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship

from app.database.base import Base
from app.mixins.timestamp import TimestampMixin

class TrainLocation(TimestampMixin, Base):

    __tablename__ = "train_locations"
                                                                      
    __table_args__ = (UniqueConstraint("train_id", name="uq_train_locations_train_id"),)

    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True
    )

    train_id: Mapped[int] = mapped_column(
        ForeignKey("trains.id")
    )

    station_id: Mapped[int] = mapped_column(
        ForeignKey("stations.id")
    )

    next_station_id: Mapped[int | None] = mapped_column(
        ForeignKey("stations.id"),
        nullable=True,
    )

    progress_ratio: Mapped[float] = mapped_column(
        Float,
        default=0.0,
    )

    status: Mapped[str] = mapped_column(
        String(20),
        default="in_transit",
    )

    train = relationship(
        "Train",
        back_populates="locations",
        foreign_keys=[train_id],
    )

    station = relationship(
        "Station",
        foreign_keys=[station_id],
    )

    next_station = relationship(
        "Station",
        foreign_keys=[next_station_id],
    )
