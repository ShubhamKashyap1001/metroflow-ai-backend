from datetime import datetime, timezone

from sqlalchemy import String
from sqlalchemy import DateTime

from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column

from app.database.base import Base

class Route(Base):

    __tablename__ = "routes"

    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True
    )

    route_name: Mapped[str] = mapped_column(
        String(100),
        unique=True,
        nullable=False
    )

    start_station: Mapped[str] = mapped_column(
        String(100),
        nullable=False
    )

    end_station: Mapped[str] = mapped_column(
        String(100),
        nullable=False
    )

    # BUGFIX (timezone-aware timestamps): same class of fix as
    # Journey.checkin_time/checkout_time (app/models/journey.py) -
    # `DateTime` (naive) + `datetime.utcnow` (naive) meant this column
    # never carried timezone info, unlike every other timestamp column
    # in the project.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc)
    )