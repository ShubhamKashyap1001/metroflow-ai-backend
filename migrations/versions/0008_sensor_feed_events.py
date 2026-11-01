from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008_sensor_feed_events"
down_revision: Union[str, None] = "0007_notification_preferences"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    if "sensor_feed_events" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "sensor_feed_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source", sa.String(length=80), nullable=False),
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column("station_id", sa.Integer(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_count", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["station_id"], ["stations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source", "event_id", name="uq_sensor_feed_events_source_event"),
    )
    op.create_index(
        "ix_sensor_feed_events_station_observed",
        "sensor_feed_events",
        ["station_id", "observed_at"],
    )

def downgrade() -> None:
    if "sensor_feed_events" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("sensor_feed_events")
