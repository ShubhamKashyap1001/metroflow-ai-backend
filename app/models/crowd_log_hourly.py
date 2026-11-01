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

    __tablename__ = "crowd_logs_hourly"

    __table_args__ = (
        UniqueConstraint("station_id", "hour_bucket", name="uq_crowd_logs_hourly_station_hour"),
        Index("ix_crowd_logs_hourly_station_id_hour_bucket", "station_id", "hour_bucket"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id"))

    hour_bucket: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    avg_count: Mapped[float] = mapped_column(Float)
    max_count: Mapped[int] = mapped_column(Integer)
    min_count: Mapped[int] = mapped_column(Integer)
    sample_count: Mapped[int] = mapped_column(Integer)

    station = relationship("Station")
