from datetime import time

from sqlalchemy import Boolean
from sqlalchemy import Enum
from sqlalchemy import ForeignKey
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import Time

from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship

from app.database.base import Base
from app.enums.day_type import DayType
from app.enums.schedule_status import ScheduleStatus
from app.mixins.timestamp import TimestampMixin

class TrainSchedule(TimestampMixin, Base):

    __tablename__ = "train_schedules"

    __table_args__ = (
        Index("ix_train_schedules_station_id_day_type", "station_id", "day_type"),
        Index("ix_train_schedules_station_id_status", "station_id", "status"),
        Index("ix_train_schedules_train_id", "train_id"),
    )

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

    day_type: Mapped[DayType] = mapped_column(
        Enum(DayType),
        default=DayType.WEEKDAY
    )

    is_peak_hour: Mapped[bool] = mapped_column(
        Boolean,
        default=False
    )

    frequency_minutes: Mapped[int] = mapped_column(
        Integer,
        default=10
    )

    status: Mapped[ScheduleStatus] = mapped_column(
        Enum(ScheduleStatus),
        default=ScheduleStatus.ON_TIME
    )

    delay_minutes: Mapped[int] = mapped_column(
        Integer,
        default=0
    )

    actual_arrival_time: Mapped[time | None] = mapped_column(
        Time,
        nullable=True
    )

    actual_departure_time: Mapped[time | None] = mapped_column(
        Time,
        nullable=True
    )

    train = relationship(
        "Train",
        back_populates="schedules"
    )

    station = relationship(
        "Station"
    )
