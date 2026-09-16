"""Add saved_routes table - a user's saved "daily route" for the home
page personalization widget (app/models/saved_route.py).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002_saved_routes"
down_revision: Union[str, None] = "0001_baseline_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "saved_routes" not in inspector.get_table_names():
        op.create_table(
            "saved_routes",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("user_id", sa.UUID(), nullable=False),
            sa.Column("origin_station_id", sa.Integer(), nullable=False),
            sa.Column("destination_station_id", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
            sa.ForeignKeyConstraint(["destination_station_id"], ["stations.id"]),
            sa.ForeignKeyConstraint(["origin_station_id"], ["stations.id"]),
            sa.ForeignKeyConstraint(["user_id"], ["user_profiles.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("user_id", name="ux_saved_routes_user_id"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "saved_routes" in inspector.get_table_names():
        op.drop_table("saved_routes")