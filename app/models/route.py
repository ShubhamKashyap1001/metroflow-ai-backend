from datetime import datetime

from sqlalchemy import String
from sqlalchemy import DateTime

from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column

from app.database.database import Base


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

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow
    )