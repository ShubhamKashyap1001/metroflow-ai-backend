from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004_crowd_response_actions"
down_revision: Union[str, None] = "0003_incident_workflow"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    op.create_table(
        "crowd_response_actions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("station_id", sa.Integer(), nullable=False),
        sa.Column("action_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="open", nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("created_by_id", sa.UUID(), nullable=False),
        sa.Column("assigned_to_id", sa.UUID(), nullable=True),
        sa.Column("linked_alert_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["station_id"], ["stations.id"]),
        sa.ForeignKeyConstraint(["created_by_id"], ["user_profiles.id"]),
        sa.ForeignKeyConstraint(["assigned_to_id"], ["user_profiles.id"]),
        sa.ForeignKeyConstraint(["linked_alert_id"], ["alerts.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_crowd_response_actions_station_created", "crowd_response_actions", ["station_id", "created_at"])

def downgrade() -> None:
    op.drop_index("ix_crowd_response_actions_station_created", table_name="crowd_response_actions")
    op.drop_table("crowd_response_actions")
