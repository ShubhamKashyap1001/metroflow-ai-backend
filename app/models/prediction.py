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

    # BUGFIX (expensive analytics queries): this table had no indexes at
    # all beyond the primary key, yet it's written to continuously (every
    # crowd/demand/delay/frequency prediction ever made) and read by
    # analytics_service.py::prediction_insights() ordered by created_at
    # DESC on every "AI Prediction dashboard" load - that was a full
    # table sort of the entire, ever-growing predictions history on
    # every call.
    __table_args__ = (
        Index("ix_predictions_created_at", "created_at"),
        # BUGFIX (unnecessary prediction DB writes): supports the
        # get-or-create lookup in prediction_service._get_or_save_prediction
        # - on a value-cache hit that function looks up the row an
        # earlier call already wrote for this exact (station_id,
        # prediction_type, target_datetime) instead of inserting a
        # duplicate. Without this index that lookup is itself a full
        # table scan on every cache hit (i.e. most calls) - would trade
        # one problem (unbounded duplicate writes) for another
        # (unbounded scan cost per read).
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

    confidence: Mapped[float] = mapped_column(
        Float,
        default=0
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
