from sqlalchemy import Enum
from sqlalchemy import ForeignKey
from sqlalchemy import String

from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship

from app.database.base import Base
from app.enums.alert_type import AlertType
from app.mixins.timestamp import TimestampMixin


class Alert(TimestampMixin, Base):

    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(primary_key=True)

    station_id: Mapped[int] = mapped_column(
        ForeignKey("stations.id")
    )

    alert_type: Mapped[AlertType] = mapped_column(
        Enum(AlertType)
    )

    message: Mapped[str] = mapped_column(
        String(500)
    )

    station = relationship("Station")