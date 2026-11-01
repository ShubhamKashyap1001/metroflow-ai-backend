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
