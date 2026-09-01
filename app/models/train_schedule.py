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
        # BUGFIX (expensive train/schedule queries): peak_hour_schedules()
        # and delayed_schedules() are dashboard KPI reads hit on
        # essentially every load, and both are called with station_id=None
        # (no city selected) far more often than filtered - that's the
        # Dispatch Board's default view. The three indexes above all lead
        # with station_id, so none of them can be used once station_id
        # isn't part of the filter: is_peak_hour=True and
        # (delay_minutes>0 OR status=DELAYED) both fell back to a full
        # Seq Scan of train_schedules on every cache-miss in that (most
        # common) case. These two plain indexes let Postgres answer both
        # global queries with an index scan (status/delay_minutes can
        # also BitmapOr together) instead of scanning every row.
        Index("ix_train_schedules_is_peak_hour", "is_peak_hour"),
        Index("ix_train_schedules_delay_minutes", "delay_minutes"),
        Index("ix_train_schedules_status", "status"),
        # PERF FIX (query-analysis pass, see docs/query-performance-and-indexing.md):
        # schedule_service.get_upcoming_schedules() - the Dispatch
        # Board's "Upcoming Train Schedule" widget, hit on essentially
        # every dashboard load - filters on day_type + departure_time
        # and then ORDER BY departure_time. None of the existing
        # indexes above lead with departure_time (or even include it),
        # so this fell back to a full Seq Scan of train_schedules
        # followed by an explicit Sort node on every call (confirmed
        # via EXPLAIN ANALYZE - see the doc for the before/after
        # plans). This composite index lets Postgres answer the filter
        # AND satisfy the ORDER BY directly from the index (no separate
        # sort step needed), the same way ix_train_schedules_station_id_day_type
        # already does for the station-scoped query above.
        Index("ix_train_schedules_day_type_departure_time", "day_type", "departure_time"),
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

    # Position of this station within the train's route (1 = first
    # stop, 2 = second, ...). This is what "next stop" resolution
    # (schedule_service.get_upcoming_schedules) uses to find the
    # correct following station - NOT sorting by departure_time,
    # which breaks the moment two different stations' scheduled
    # times happen to collide (or, before the timetable/history
    # split, when thousands of duplicate historical rows at the same
    # station shared the exact same clock time). Nullable only to
    # stay backward-compatible with rows inserted before this column
    # existed; every row inserted by seed_real_data.py going forward
    # sets it from train_operations.csv's own station_sequence column.
    station_sequence: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )

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
