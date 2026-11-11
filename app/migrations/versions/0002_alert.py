"""alert table (Google Chat alerts, SPEC §9)

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
    op.create_table('alert',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('key', sa.String(length=255), nullable=False),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('message', sa.Text(), nullable=False),
    sa.Column('opened_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('notified', sa.Boolean(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('alert', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_alert_key'), ['key'], unique=False)
        batch_op.create_index(batch_op.f('ix_alert_resolved_at'), ['resolved_at'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('alert', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_alert_resolved_at'))
        batch_op.drop_index(batch_op.f('ix_alert_key'))

    op.drop_table('alert')
