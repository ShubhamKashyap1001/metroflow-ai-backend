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
    """Live crowd state — ONE row per station, upserted in place.

    This is the "current fact" table (mirrors the pattern already used
    correctly by `TrainLocation` for live train positions): the crowd
    simulator/checkin/checkout paths UPDATE this row every tick/event
    instead of INSERTing a new one, so its size is permanently bounded
    by the number of active stations (a few hundred), never by elapsed
    time or tick count.

    `crowd_logs` (see app/models/crowd_log.py) remains the
    append-only HISTORICAL table used for trend analytics
    (inflow/outflow, 24h avg/peak, etc.) and is now written at a much
    lower, configurable sample rate (`CROWD_HISTORY_INTERVAL_SECONDS`)
    instead of on every simulator tick — see
    app/simulator/csv_replay_simulator.py and docs/crowd-live-state-and-retention.md for
    the full rationale.

    `updated_at` (from TimestampMixin) is the authoritative "as of"
    timestamp for the live dashboard/heatmap - it changes on every
    write, unlike `created_at` which is fixed at the station's first
    tick.
    """

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
