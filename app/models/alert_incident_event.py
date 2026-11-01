from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base

class AlertIncidentEvent(Base):

    __tablename__ = "alert_incident_events"
    __table_args__ = (Index("ix_alert_incident_events_alert_created", "alert_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    alert_id: Mapped[int] = mapped_column(ForeignKey("alerts.id", ondelete="CASCADE"), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(UUID(as_uuid=True), ForeignKey("user_profiles.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    alert = relationship("Alert")
    actor = relationship("UserProfile")

    @property
    def actor_name(self) -> str | None:
        return self.actor.full_name if self.actor else None
