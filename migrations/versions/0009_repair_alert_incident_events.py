import sqlalchemy as sa
from alembic import op

revision = "0009_repair_alert_incident_events"
down_revision = "0008_sensor_feed_events"
branch_labels = None
depends_on = None

def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "alert_incident_events" not in inspector.get_table_names():
        op.create_table(
            "alert_incident_events",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("alert_id", sa.Integer(), nullable=False),
            sa.Column("actor_id", sa.UUID(), nullable=True),
            sa.Column("action", sa.String(length=32), nullable=False),
            sa.Column("note", sa.Text(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(
                ["alert_id"], ["alerts.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(["actor_id"], ["user_profiles.id"]),
            sa.PrimaryKeyConstraint("id"),
        )

    index_names = {
        index["name"]
        for index in sa.inspect(bind).get_indexes("alert_incident_events")
    }
    if "ix_alert_incident_events_alert_created" not in index_names:
        op.create_index(
            "ix_alert_incident_events_alert_created",
            "alert_incident_events",
            ["alert_id", "created_at"],
        )

def downgrade() -> None:
    pass
