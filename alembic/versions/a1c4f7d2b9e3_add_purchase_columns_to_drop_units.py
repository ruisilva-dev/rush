"""add purchase columns to drop_units

Revision ID: a1c4f7d2b9e3
Revises: 55e2e23a21c9
Create Date: 2026-10-27 12:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a1c4f7d2b9e3"
down_revision: Union[str, Sequence[str], None] = "55e2e23a21c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "drop_units", sa.Column("purchased_by_user_id", sa.Uuid(), nullable=True)
    )
    op.add_column(
        "drop_units",
        sa.Column("purchased_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_drop_units_purchased_by_user_id",
        "drop_units",
        ["purchased_by_user_id"],
        unique=False,
        postgresql_where=sa.text("purchased_by_user_id IS NOT NULL"),
    )
    # Backfill historical purchased records
    op.execute(
        """
        UPDATE drop_units
        SET purchased_by_user_id = held_by_user_id,
            held_by_user_id = NULL
        WHERE status = 'purchased';
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    # Restore the legacy overloaded representation before dropping the columns
    op.execute(
        """
        UPDATE drop_units
        SET held_by_user_id = purchased_by_user_id
        WHERE status = 'purchased';
        """
    )
    op.drop_index(
        "ix_drop_units_purchased_by_user_id",
        table_name="drop_units",
        postgresql_where=sa.text("purchased_by_user_id IS NOT NULL"),
    )
    op.drop_column("drop_units", "purchased_at")
    op.drop_column("drop_units", "purchased_by_user_id")
