from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006_reports_and_freshness"
down_revision: Union[str, None] = "0005_nullable_prediction_confidence"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "report_schedules" not in inspector.get_table_names():
        op.create_table(
            "report_schedules",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("owner_id", sa.UUID(), nullable=False),
            sa.Column("frequency", sa.String(length=12), nullable=False),
            sa.Column("delivery_hour_utc", sa.Integer(), nullable=False),
            sa.Column("day_of_week", sa.Integer(), nullable=True),
            sa.Column("period_days", sa.Integer(), nullable=False, server_default="7"),
            sa.Column("station_id", sa.Integer(), nullable=True),
            sa.Column("line_name", sa.String(length=100), nullable=True),
            sa.Column("state", sa.String(length=50), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_run_status", sa.String(length=16), nullable=True),
            sa.Column("run_attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_run_error", sa.String(length=500), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(["owner_id"], ["user_profiles.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["station_id"], ["stations.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )
        inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("report_schedules")}
    if "run_attempts" not in columns:
        op.add_column(
            "report_schedules",
            sa.Column("run_attempts", sa.Integer(), nullable=False, server_default="0"),
        )
    indexes = {index["name"] for index in inspector.get_indexes("report_schedules")}
    if "ix_report_schedules_active_next_run" not in indexes:
        op.create_index("ix_report_schedules_active_next_run", "report_schedules", ["is_active", "next_run_at"])
    if "ix_report_schedules_owner_created" not in indexes:
        op.create_index("ix_report_schedules_owner_created", "report_schedules", ["owner_id", "created_at"])

def downgrade() -> None:
    bind = op.get_bind()
    if "report_schedules" in sa.inspect(bind).get_table_names():
        indexes = {index["name"] for index in sa.inspect(bind).get_indexes("report_schedules")}
        if "ix_report_schedules_owner_created" in indexes:
            op.drop_index("ix_report_schedules_owner_created", table_name="report_schedules")
        if "ix_report_schedules_active_next_run" in indexes:
            op.drop_index("ix_report_schedules_active_next_run", table_name="report_schedules")
        op.drop_table("report_schedules")
