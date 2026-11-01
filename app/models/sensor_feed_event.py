from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.mixins.timestamp import TimestampMixin

class SensorFeedEvent(TimestampMixin, Base):
    __tablename__ = "sensor_feed_events"
    __table_args__ = (
        UniqueConstraint("source", "event_id", name="uq_sensor_feed_events_source_event"),
        Index("ix_sensor_feed_events_station_observed", "station_id", "observed_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(80), nullable=False)
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id", ondelete="CASCADE"), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    observed_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
