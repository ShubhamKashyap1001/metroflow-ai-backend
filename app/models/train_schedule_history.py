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
    """One row per REAL, dated trip-stop - the day-by-day operational
    record (from train_operations.csv), as opposed to TrainSchedule,
    which is the recurring timetable (one row per train/station/
    day_type slot).

    Why this table exists: TrainSchedule used to hold BOTH concepts at
    once, because it only stores a time-of-day with no date. Seeding
    ~a year of real daily operations straight into it produced
    thousands of near-duplicate rows for the same slot (one per
    calendar day it ran), which is what caused the "Upcoming Train
    Schedule" widget to show the same train/station repeated dozens
    of times, and caused "next stop" lookups to occasionally resolve
    to another day's row for the SAME station instead of the true
    next station on the route.

    This table is the correct home for that day-by-day data: full
    granularity is preserved (nothing was thrown away), it's what
    app/ai_engine/prediction/*.py should train delay/crowd/frequency
    models against, and it's additive going forward - a nightly job
    (or the live simulator) can keep inserting one row per train per
    station per day here without ever touching the timetable table.
    """

    __tablename__ = "train_schedule_history"

    __table_args__ = (
        Index("ix_tsh_train_id_service_date", "train_id", "service_date"),
        Index("ix_tsh_station_id_service_date", "station_id", "service_date"),
        Index("ix_tsh_trip_id", "trip_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Groups every station-stop of one real, dated journey together -
    # the raw CSV's own trip_id (e.g. "TP-008689"). Lets a future
    # "replay this exact trip" or "route taken on this day" view be
    # built directly off this table without guessing at grouping.
    trip_id: Mapped[str] = mapped_column(String(32))

    train_id: Mapped[int] = mapped_column(ForeignKey("trains.id"))
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id"))

    # The calendar date this specific stop actually happened on - the
    # column TrainSchedule is missing, and the entire reason this
    # table exists as separate from it.
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
