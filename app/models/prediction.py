from datetime import datetime

from sqlalchemy import DateTime
from sqlalchemy import Enum
from sqlalchemy import Float
from sqlalchemy import ForeignKey
from sqlalchemy import Index
from sqlalchemy import Integer

from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship

from app.database.base import Base
from app.enums.prediction_type import PredictionType
from app.mixins.timestamp import TimestampMixin

class Prediction(TimestampMixin, Base):

    __tablename__ = "predictions"

    __table_args__ = (
        Index("ix_predictions_created_at", "created_at"),
        Index(
            "ix_predictions_station_id_type_target",
            "station_id", "prediction_type", "target_datetime",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    station_id: Mapped[int] = mapped_column(
        ForeignKey("stations.id")
    )

    predicted_count: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True
    )

    confidence: Mapped[float | None] = mapped_column(
        Float,
        default=None,
        nullable=True,
    )

    prediction_type: Mapped[PredictionType] = mapped_column(
        Enum(PredictionType),
        default=PredictionType.CROWD
    )

    predicted_value: Mapped[float] = mapped_column(
        Float,
        default=0
    )

    target_datetime: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True
    )

    model_version: Mapped[str | None] = mapped_column(
        nullable=True
    )

    station = relationship("Station")
