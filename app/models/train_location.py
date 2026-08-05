from sqlalchemy import ForeignKey

from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship

from app.database.base import Base
from app.mixins.timestamp import TimestampMixin


class TrainLocation(TimestampMixin, Base):

    __tablename__ = "train_locations"

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

    train = relationship(
        "Train",
        back_populates="locations"
    )

    station = relationship(
        "Station"
    )