from sqlalchemy import Boolean
from sqlalchemy import Enum
from sqlalchemy import String

from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship

from app.database.base import Base
from app.enums.line_status import LineStatus
from app.mixins.timestamp import TimestampMixin


class MetroLine(TimestampMixin, Base):

    __tablename__ = "metro_lines"

    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True
    )

    line_code: Mapped[str] = mapped_column(
        String(20),
        unique=True,
        nullable=False
    )

    line_name: Mapped[str] = mapped_column(
        String(100),
        nullable=False
    )

    color: Mapped[str] = mapped_column(
        String(20),
        nullable=False
    )

    status: Mapped[LineStatus] = mapped_column(
        Enum(LineStatus),
        default=LineStatus.ACTIVE
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True
    )

    stations = relationship(
        "LineStation",
        back_populates="line"
    )