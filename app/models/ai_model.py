from sqlalchemy import Float
from sqlalchemy import String

from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column

from app.database.base import Base
from app.mixins.timestamp import TimestampMixin


class AIModel(TimestampMixin, Base):

    __tablename__ = "ai_models"

    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True
    )

    model_name: Mapped[str] = mapped_column(
        String(100),
        nullable=False
    )

    version: Mapped[str] = mapped_column(
        String(30),
        nullable=False
    )

    accuracy: Mapped[float] = mapped_column(
        Float,
        default=0
    )