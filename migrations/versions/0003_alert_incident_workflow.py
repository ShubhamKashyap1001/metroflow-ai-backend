from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003_incident_workflow"
down_revision: Union[str, None] = "0002_saved_routes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    op.add_column("alerts", sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("alerts", sa.Column("acknowledged_by_id", sa.UUID(), nullable=True))
    op.add_column("alerts", sa.Column("assigned_to_id", sa.UUID(), nullable=True))
    op.create_foreign_key("fk_alerts_acknowledged_by_id_user_profiles", "alerts", "user_profiles", ["acknowledged_by_id"], ["id"])
    op.create_foreign_key("fk_alerts_assigned_to_id_user_profiles", "alerts", "user_profiles", ["assigned_to_id"], ["id"])
    op.create_table(
        "alert_incident_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("alert_id", sa.Integer(), nullable=False),
        sa.Column("actor_id", sa.UUID(), nullable=True),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["alert_id"], ["alerts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_id"], ["user_profiles.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_alert_incident_events_alert_created", "alert_incident_events", ["alert_id", "created_at"])

def downgrade() -> None:
    op.drop_index("ix_alert_incident_events_alert_created", table_name="alert_incident_events")
    op.drop_table("alert_incident_events")
    op.drop_constraint("fk_alerts_assigned_to_id_user_profiles", "alerts", type_="foreignkey")
    op.drop_constraint("fk_alerts_acknowledged_by_id_user_profiles", "alerts", type_="foreignkey")
    op.drop_column("alerts", "assigned_to_id")
    op.drop_column("alerts", "acknowledged_by_id")
    op.drop_column("alerts", "acknowledged_at")
