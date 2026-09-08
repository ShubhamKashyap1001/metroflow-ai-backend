from datetime import datetime

from sqlalchemy import DateTime
from sqlalchemy import Enum
from sqlalchemy import ForeignKey
from sqlalchemy import Float
from sqlalchemy import Index
from sqlalchemy import text

from sqlalchemy.dialects.postgresql import UUID

from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship

from app.database.base import Base
from app.enums.journey_status import JourneyStatus
from app.mixins.timestamp import TimestampMixin

class Journey(TimestampMixin, Base):

    __tablename__ = "journeys"

    __table_args__ = (
        Index("ix_journeys_user_id_status", "user_id", "status"),
        Index("ix_journeys_status_checkin_time", "status", "checkin_time"),
        Index("ix_journeys_source_station_id_status", "source_station_id", "status"),
        Index("ix_journeys_destination_station_id_status", "destination_station_id", "status"),
        # Enforces "one ACTIVE journey per user" at the DB level so two
        # concurrent check-in requests can't both pass the
        # active_journey_for_user() pre-check and both insert an ACTIVE
        # row (check-then-insert race). The second insert now fails
        # with an IntegrityError, which journey_service.check_in()
        # catches and turns into the normal "already have an active
        # journey" 400 response.
        #
        # BUGFIX (production Postgres schema creation was completely
        # broken): this predicate must use the actual value Postgres
        # stores for JourneyStatus.ACTIVE, not JourneyStatus.ACTIVE's
        # Python-side `.value`. `Enum(JourneyStatus)` below has no
        # `values_callable`, so SQLAlchemy's default behaviour creates
        # the native Postgres enum type using each member's NAME
        # ("ACTIVE"/"COMPLETED"/"CANCELLED"), not its lowercase
        # `.value` ("active"/"completed"/"cancelled") - and that's also
        # what gets bound whenever ORM code writes/queries
        # `JourneyStatus.ACTIVE` (see journey_service.py). The lowercase
        # `'active'` this literal used to contain is not a valid label
        # of that enum type at all, so on a real Postgres database this
        # index (and therefore `Base.metadata.create_all()` /
        # migrations/versions/0001_baseline_schema.py in their
        # entirety) failed outright with `invalid input value for enum
        # journeystatus: "active"` - the app could never even create
        # its schema. SQLite silently accepted the lowercase literal
        # (no real enum type there), which is why this stayed hidden
        # through every prior sqlite-based verification.
        Index(
            "ux_journeys_one_active_per_user",
            "user_id",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
        ),
    )

    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True
    )

    user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user_profiles.id"),
        nullable=False
    )

    source_station_id: Mapped[int] = mapped_column(
        ForeignKey("stations.id"),
        nullable=False
    )

    destination_station_id: Mapped[int] = mapped_column(
        ForeignKey("stations.id"),
        nullable=False
    )

    # BUGFIX (timezone-aware timestamps): these were plain `DateTime`
    # (timestamp WITHOUT time zone) even though journey_service.py has
    # always written them with `datetime.now(timezone.utc)` (aware).
    # Postgres silently drops the aware value's offset info on the way
    # in, and every value read back out is a *naive* datetime - which
    # then gets JSON-serialized with no UTC offset (e.g.
    # "2026-09-03T10:15:30" instead of "...+00:00"). The frontend's
    # `new Date(...)` calls (EnquiryCenter.tsx-style timestamp
    # rendering, "Passenger Entry & Exit Records") parse an
    # offset-less ISO string as *local browser time*, not UTC - a
    # user checking in at 10:15 UTC could see their own check-in time
    # rendered as anywhere from 4am to 11pm depending on their
    # timezone. `DateTime(timezone=True)` (timestamp WITH time zone)
    # matches every other timestamp column in this project (see
    # app/mixins/timestamp.py's TimestampMixin and the other
    # DateTime(timezone=True) columns across app/models/) and keeps
    # what's stored/returned actually UTC-aware end to end.
    checkin_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False
    )

    checkout_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True
    )

    fare: Mapped[float] = mapped_column(
        Float,
        default=0
    )

    status: Mapped[JourneyStatus] = mapped_column(
        Enum(JourneyStatus),
        default=JourneyStatus.ACTIVE
    )

    user = relationship(
        "UserProfile",
        back_populates="journeys"
    )

    source_station = relationship(
        "Station",
        foreign_keys=[source_station_id],
        back_populates="journeys_from"
    )

    destination_station = relationship(
        "Station",
        foreign_keys=[destination_station_id],
        back_populates="journeys_to"
    )