"""make crisis_event_id nullable in recommendation_runs

Revision ID: 2b8214277b0e
Revises: 1a1614266a9d
Create Date: 2026-11-21 00:15:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2b8214277b0e'
down_revision: Union[str, None] = '1a1614266a9d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        'recommendation_runs',
        'crisis_event_id',
        existing_type=sa.Uuid(),
        nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        'recommendation_runs',
        'crisis_event_id',
        existing_type=sa.Uuid(),
        nullable=False,
    )
