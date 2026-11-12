"""API keys get a role and campus scope instead of can_write (which meant admin)

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("api_key", schema=None) as batch_op:
        batch_op.add_column(sa.Column("role", sa.String(length=16), nullable=False,
                                      server_default="viewer"))
        batch_op.add_column(sa.Column("campus_ids", sa.JSON(), nullable=True))
    # Keep what existing keys could already do: can_write meant admin.
    op.execute("UPDATE api_key SET role = 'admin' WHERE can_write")
    with op.batch_alter_table("api_key", schema=None) as batch_op:
        batch_op.drop_column("can_write")


def downgrade() -> None:
    with op.batch_alter_table("api_key", schema=None) as batch_op:
        batch_op.add_column(sa.Column("can_write", sa.Boolean(), nullable=False,
                                      server_default=sa.false()))
    op.execute("UPDATE api_key SET can_write = (role <> 'viewer')")
    with op.batch_alter_table("api_key", schema=None) as batch_op:
        batch_op.drop_column("campus_ids")
        batch_op.drop_column("role")
