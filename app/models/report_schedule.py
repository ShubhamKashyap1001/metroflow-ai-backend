from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base
from app.mixins.timestamp import TimestampMixin

class ReportSchedule(TimestampMixin, Base):
    __tablename__ = "report_schedules"
    __table_args__ = (
        Index("ix_report_schedules_active_next_run", "is_active", "next_run_at"),
        Index("ix_report_schedules_owner_created", "owner_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    owner_id: Mapped[str] = mapped_column(UUID(as_uuid=True), ForeignKey("user_profiles.id", ondelete="CASCADE"), nullable=False)
    frequency: Mapped[str] = mapped_column(String(12), nullable=False)
    delivery_hour_utc: Mapped[int] = mapped_column(Integer, nullable=False)
    day_of_week: Mapped[int | None] = mapped_column(Integer, nullable=True)
    period_days: Mapped[int] = mapped_column(Integer, nullable=False, default=7)
    station_id: Mapped[int | None] = mapped_column(ForeignKey("stations.id", ondelete="SET NULL"), nullable=True)
    line_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    state: Mapped[str | None] = mapped_column(String(50), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    next_run_at: Mapped[datetime] = mapped_column(nullable=False)
    last_run_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_run_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    run_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_run_error: Mapped[str | None] = mapped_column(String(500), nullable=True)

    owner = relationship("UserProfile")
    station = relationship("Station")
