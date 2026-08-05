from sqlalchemy import Float
from sqlalchemy import ForeignKey
from sqlalchemy import Integer

from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship

from app.database.base import Base
from app.mixins.timestamp import TimestampMixin


class Prediction(TimestampMixin, Base):

    __tablename__ = "predictions"

    id: Mapped[int] = mapped_column(primary_key=True)

    station_id: Mapped[int] = mapped_column(
        ForeignKey("stations.id")
    )

    predicted_count: Mapped[int] = mapped_column(
        Integer
    )

    confidence: Mapped[float] = mapped_column(
        Float
    )

    station = relationship("Station")