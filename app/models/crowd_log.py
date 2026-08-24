from sqlalchemy import Enum
from sqlalchemy import ForeignKey
from sqlalchemy import Index
from sqlalchemy import Integer

from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship

from app.database.base import Base
from app.enums.crowd_level import CrowdLevel
from app.mixins.timestamp import TimestampMixin

class CrowdLog(TimestampMixin, Base):

    __tablename__ = "crowd_logs"

    __table_args__ = (
        Index(
            "ix_crowd_logs_station_id_created_at",
            "station_id",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    station_id: Mapped[int] = mapped_column(
        ForeignKey("stations.id")
    )

    current_count: Mapped[int] = mapped_column(
        Integer
    )

    crowd_level: Mapped[CrowdLevel] = mapped_column(
        Enum(CrowdLevel),
        default=CrowdLevel.LOW
    )

    station = relationship("Station")
