from sqlalchemy import Boolean
from sqlalchemy import Enum
from sqlalchemy import Integer
from sqlalchemy import String

from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship

from app.database.base import Base
from app.enums.train_status import TrainStatus
from app.mixins.timestamp import TimestampMixin


class Train(TimestampMixin, Base):

    __tablename__ = "trains"

    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True
    )

    train_number: Mapped[str] = mapped_column(
        String(30),
        unique=True,
        nullable=False
    )

    capacity: Mapped[int] = mapped_column(
        Integer,
        nullable=False
    )

    status: Mapped[TrainStatus] = mapped_column(
        Enum(TrainStatus),
        default=TrainStatus.ACTIVE
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True
    )

    schedules = relationship(
        "TrainSchedule",
        back_populates="train"
    )

    locations = relationship(
        "TrainLocation",
        back_populates="train"
    )