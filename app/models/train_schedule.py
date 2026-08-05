from datetime import time

from sqlalchemy import ForeignKey
from sqlalchemy import Time

from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship

from app.database.base import Base


class TrainSchedule(Base):

    __tablename__ = "train_schedules"

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

    arrival_time: Mapped[time]
    departure_time: Mapped[time]
    platform_number: Mapped[int]

    train = relationship(
        "Train",
        back_populates="schedules"
    )

    station = relationship(
        "Station"
    )