from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005_nullable_prediction_confidence"
down_revision: Union[str, None] = "0004_crowd_response_actions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, None] = None

def upgrade() -> None:
    op.alter_column(
        "predictions",
        "confidence",
        existing_type=sa.Float(),
        nullable=True,
        existing_nullable=False,
    )

    op.execute(
        "UPDATE predictions SET confidence = NULL "
        "WHERE prediction_type IN ('DELAY', 'FREQUENCY') "
        "OR model_version = 'heuristic_fallback'"
    )

def downgrade() -> None:
    op.execute("UPDATE predictions SET confidence = 0 WHERE confidence IS NULL")
    op.alter_column(
        "predictions",
        "confidence",
        existing_type=sa.Float(),
        nullable=False,
        existing_nullable=True,
    )
