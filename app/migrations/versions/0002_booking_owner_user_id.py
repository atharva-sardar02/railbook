"""booking owner (user_id)

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable, no default: Postgres only records the new column in its catalog, without
    # rewriting the table, and the API version still running keeps working (it never reads
    # or writes user_id). Existing rows get NULL: no owner, visible to admins only.
    op.add_column("bookings", sa.Column("user_id", sa.String(length=255), nullable=True))
    op.create_index(op.f("ix_bookings_user_id"), "bookings", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_bookings_user_id"), table_name="bookings")
    op.drop_column("bookings", "user_id")
