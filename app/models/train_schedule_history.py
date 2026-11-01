from datetime import date, time

from sqlalchemy import Date
from sqlalchemy import Float
from sqlalchemy import ForeignKey
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import Time

from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship

from app.database.base import Base
from app.mixins.timestamp import TimestampMixin

class TrainScheduleHistory(TimestampMixin, Base):

    __tablename__ = "train_schedule_history"

    __table_args__ = (
        Index("ix_tsh_train_id_service_date", "train_id", "service_date"),
        Index("ix_tsh_station_id_service_date", "station_id", "service_date"),
        Index("ix_tsh_trip_id", "trip_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    trip_id: Mapped[str] = mapped_column(String(32))

    train_id: Mapped[int] = mapped_column(ForeignKey("trains.id"))
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id"))

    service_date: Mapped[date] = mapped_column(Date)

    station_sequence: Mapped[int] = mapped_column(Integer)

    scheduled_arrival: Mapped[time] = mapped_column(Time)
    scheduled_departure: Mapped[time] = mapped_column(Time)
    actual_arrival: Mapped[time | None] = mapped_column(Time, nullable=True)
    actual_departure: Mapped[time | None] = mapped_column(Time, nullable=True)

    delay_arrival_min: Mapped[float] = mapped_column(Float, default=0.0)
    delay_departure_min: Mapped[float] = mapped_column(Float, default=0.0)

    passenger_density: Mapped[str | None] = mapped_column(String(16), nullable=True)
    weather: Mapped[str | None] = mapped_column(String(32), nullable=True)
    delay_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)

    train = relationship("Train")
    station = relationship("Station")
