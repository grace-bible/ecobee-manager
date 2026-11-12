"""runtime_sample (ecobee 5-minute history); thermostat timezone + active ecobee alerts

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('runtime_sample',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('thermostat_id', sa.Integer(), nullable=False),
    sa.Column('at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('temp', sa.Float(), nullable=True),
    sa.Column('humidity', sa.Integer(), nullable=True),
    sa.Column('heat_sp', sa.Float(), nullable=True),
    sa.Column('cool_sp', sa.Float(), nullable=True),
    sa.Column('outdoor', sa.Float(), nullable=True),
    sa.Column('heat_s', sa.Integer(), nullable=False),
    sa.Column('cool_s', sa.Integer(), nullable=False),
    sa.Column('fan_s', sa.Integer(), nullable=False),
    sa.Column('occupied', sa.Boolean(), nullable=True),
    sa.ForeignKeyConstraint(['thermostat_id'], ['thermostat.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('thermostat_id', 'at')
    )
    with op.batch_alter_table('runtime_sample', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_runtime_sample_at'), ['at'], unique=False)
        batch_op.create_index(batch_op.f('ix_runtime_sample_thermostat_id'), ['thermostat_id'],
                              unique=False)

    with op.batch_alter_table('thermostat', schema=None) as batch_op:
        batch_op.add_column(sa.Column('timezone', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('ecobee_alerts', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('thermostat', schema=None) as batch_op:
        batch_op.drop_column('ecobee_alerts')
        batch_op.drop_column('timezone')

    with op.batch_alter_table('runtime_sample', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_runtime_sample_thermostat_id'))
        batch_op.drop_index(batch_op.f('ix_runtime_sample_at'))

    op.drop_table('runtime_sample')
