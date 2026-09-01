from datetime import datetime

from sqlalchemy import DateTime
from sqlalchemy import Float
from sqlalchemy import ForeignKey
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import UniqueConstraint

from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship

from app.database.base import Base
from app.mixins.timestamp import TimestampMixin


class CrowdLogHourly(TimestampMixin, Base):
    """Hourly rollup of `crowd_logs`, used as the retention job's
    aggregation target.

    The retention job (app/simulator/retention.py) periodically
    aggregates raw `crowd_logs` rows older than
    `settings.CROWD_LOG_ROLLUP_AFTER_DAYS` into one row per
    (station_id, hour_bucket) here — preserving avg/max/min/sample
    count for long-range analytics/trend charts — and then deletes the
    raw rows it just rolled up. This keeps the raw, high-resolution
    `crowd_logs` table bounded to a small recent window while never
    losing the ability to answer "what was station X like last month".

    One row per station per hour (324 stations x 24h = ~7.8k rows/day
    at steady state, vs. hundreds of thousands of raw rows/day before
    rollup+deletion) — see docs/crowd-live-state-and-retention.md for the full
    before/after numbers.
    """

    __tablename__ = "crowd_logs_hourly"

    __table_args__ = (
        UniqueConstraint("station_id", "hour_bucket", name="uq_crowd_logs_hourly_station_hour"),
        Index("ix_crowd_logs_hourly_station_id_hour_bucket", "station_id", "hour_bucket"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id"))

    # Start of the hour this row summarizes (UTC, truncated to the hour).
    hour_bucket: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    avg_count: Mapped[float] = mapped_column(Float)
    max_count: Mapped[int] = mapped_column(Integer)
    min_count: Mapped[int] = mapped_column(Integer)
    sample_count: Mapped[int] = mapped_column(Integer)

    station = relationship("Station")
