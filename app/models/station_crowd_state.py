from sqlalchemy import Enum
from sqlalchemy import ForeignKey
from sqlalchemy import Integer

from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship

from app.database.base import Base
from app.enums.crowd_level import CrowdLevel
from app.mixins.timestamp import TimestampMixin

class StationCrowdState(TimestampMixin, Base):

    __tablename__ = "station_crowd_state"

    station_id: Mapped[int] = mapped_column(
        ForeignKey("stations.id"),
        primary_key=True,
    )

    current_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
    )

    crowd_level: Mapped[CrowdLevel] = mapped_column(
        Enum(CrowdLevel),
        default=CrowdLevel.LOW,
    )

    station = relationship("Station")
