from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base
from app.mixins.timestamp import TimestampMixin

class CrowdResponseAction(TimestampMixin, Base):

    __tablename__ = "crowd_response_actions"
    __table_args__ = (Index("ix_crowd_response_actions_station_created", "station_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id"), nullable=False)
    action_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="open")
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_by_id: Mapped[str] = mapped_column(UUID(as_uuid=True), ForeignKey("user_profiles.id"), nullable=False)
    assigned_to_id: Mapped[str | None] = mapped_column(UUID(as_uuid=True), ForeignKey("user_profiles.id"), nullable=True)
    linked_alert_id: Mapped[int | None] = mapped_column(ForeignKey("alerts.id"), nullable=True)

    station = relationship("Station")
    creator = relationship("UserProfile", foreign_keys=[created_by_id])
    assignee = relationship("UserProfile", foreign_keys=[assigned_to_id])
    linked_alert = relationship("Alert")

    @property
    def station_name(self) -> str | None:
        return self.station.station_name if self.station else None

    @property
    def assignee_name(self) -> str | None:
        return self.assignee.full_name if self.assignee else None
