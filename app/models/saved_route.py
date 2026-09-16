"""A user's saved "daily route" (home screen personalization).

Deliberately NOT the same thing as `Journey` (app/models/journey.py) -
Journey is a one-off active/completed check-in/check-out trip record.
SavedRoute is a standing preference: "I take Station A -> Station B
every day", stored once and read back on every home-page load to
drive the personalized "your route" widget (live next-departure/ETA
for that pair) without the user re-picking stations each time.

One row per user (unique on user_id): saving a new route overwrites
the old one (see saved_route_service.set_my_route's upsert), matching
the product shape - a single "your daily route" card, not a list.
"""
from sqlalchemy import ForeignKey
from sqlalchemy import Integer
from sqlalchemy import UniqueConstraint

from sqlalchemy.dialects.postgresql import UUID

from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship

from app.database.base import Base
from app.mixins.timestamp import TimestampMixin


class SavedRoute(TimestampMixin, Base):

    __tablename__ = "saved_routes"

    __table_args__ = (
        UniqueConstraint("user_id", name="ux_saved_routes_user_id"),
    )

    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True,
    )

    user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user_profiles.id"),
        nullable=False,
    )

    origin_station_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("stations.id"),
        nullable=False,
    )

    destination_station_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("stations.id"),
        nullable=False,
    )

    user = relationship("UserProfile")

    origin_station = relationship(
        "Station",
        foreign_keys=[origin_station_id],
    )

    destination_station = relationship(
        "Station",
        foreign_keys=[destination_station_id],
    )