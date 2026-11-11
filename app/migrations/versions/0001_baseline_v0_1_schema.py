"""baseline v0.1 schema

Revision ID: 0001
Revises: 
Create Date: 2026-11-11 13:09:49.197173
"""

import sqlalchemy as sa
from alembic import op

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('api_key',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('key_hash', sa.String(length=128), nullable=False),
    sa.Column('can_write', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('key_hash'),
    sa.UniqueConstraint('name')
    )
    op.create_table('app_user',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('email', sa.String(length=255), nullable=False),
    sa.Column('role', sa.String(length=16), nullable=False),
    sa.Column('campus_ids', sa.JSON(), nullable=True),
    sa.Column('last_login', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('email')
    )
    op.create_table('audit_log',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('actor', sa.String(length=255), nullable=False),
    sa.Column('source', sa.String(length=16), nullable=False),
    sa.Column('action', sa.String(length=64), nullable=False),
    sa.Column('target', sa.String(length=255), nullable=False),
    sa.Column('detail', sa.JSON(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('audit_log', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_audit_log_at'), ['at'], unique=False)

    op.create_table('calendar_event',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('calendar_id', sa.String(length=255), nullable=False),
    sa.Column('event_id', sa.String(length=255), nullable=False),
    sa.Column('title', sa.String(length=500), nullable=True),
    sa.Column('starts_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('ends_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('hvac_tag', sa.JSON(), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('calendar_id', 'event_id')
    )
    with op.batch_alter_table('calendar_event', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_calendar_event_calendar_id'), ['calendar_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_calendar_event_ends_at'), ['ends_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_calendar_event_starts_at'), ['starts_at'], unique=False)

    op.create_table('calendar_sync_state',
    sa.Column('calendar_id', sa.String(length=255), nullable=False),
    sa.Column('sync_token', sa.Text(), nullable=True),
    sa.Column('last_success', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('calendar_id')
    )
    op.create_table('campus',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('timezone', sa.String(length=64), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name')
    )
    op.create_table('ecobee_credentials',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('refresh_token_enc', sa.Text(), nullable=True),
    sa.Column('access_token_enc', sa.Text(), nullable=True),
    sa.Column('last_login', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('zone_group',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name')
    )
    op.create_table('zone',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('campus_id', sa.Integer(), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('calendar_id', sa.String(length=255), nullable=True),
    sa.Column('calendar_name', sa.String(length=255), nullable=True),
    sa.Column('lead_minutes', sa.Integer(), nullable=False),
    sa.Column('tail_minutes', sa.Integer(), nullable=False),
    sa.Column('occupied_heat', sa.Float(), nullable=False),
    sa.Column('occupied_cool', sa.Float(), nullable=False),
    sa.Column('unoccupied_heat', sa.Float(), nullable=False),
    sa.Column('unoccupied_cool', sa.Float(), nullable=False),
    sa.Column('min_setpoint', sa.Float(), nullable=False),
    sa.Column('max_setpoint', sa.Float(), nullable=False),
    sa.ForeignKeyConstraint(['campus_id'], ['campus.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('campus_id', 'name')
    )
    with op.batch_alter_table('zone', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_zone_calendar_id'), ['calendar_id'], unique=False)

    op.create_table('group_zone',
    sa.Column('group_id', sa.Integer(), nullable=False),
    sa.Column('zone_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['group_id'], ['zone_group.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['zone_id'], ['zone.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('group_id', 'zone_id')
    )
    op.create_table('schedule_block',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('zone_id', sa.Integer(), nullable=True),
    sa.Column('group_id', sa.Integer(), nullable=True),
    sa.Column('weekdays', sa.Integer(), nullable=False),
    sa.Column('start', sa.Time(), nullable=False),
    sa.Column('end', sa.Time(), nullable=False),
    sa.Column('mode', sa.String(length=16), nullable=False),
    sa.Column('heat', sa.Float(), nullable=True),
    sa.Column('cool', sa.Float(), nullable=True),
    sa.Column('label', sa.String(length=100), nullable=True),
    sa.ForeignKeyConstraint(['group_id'], ['zone_group.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['zone_id'], ['zone.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('thermostat',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('identifier', sa.String(length=32), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('model', sa.String(length=64), nullable=True),
    sa.Column('campus_id', sa.Integer(), nullable=True),
    sa.Column('zone_id', sa.Integer(), nullable=True),
    sa.Column('connected', sa.Boolean(), nullable=False),
    sa.Column('current_temp', sa.Float(), nullable=True),
    sa.Column('humidity', sa.Integer(), nullable=True),
    sa.Column('heat_setpoint', sa.Float(), nullable=True),
    sa.Column('cool_setpoint', sa.Float(), nullable=True),
    sa.Column('hvac_mode', sa.String(length=16), nullable=True),
    sa.Column('equipment_running', sa.String(length=200), nullable=True),
    sa.Column('active_hold', sa.JSON(), nullable=True),
    sa.Column('revision', sa.String(length=64), nullable=True),
    sa.Column('app_hold_heat', sa.Float(), nullable=True),
    sa.Column('app_hold_cool', sa.Float(), nullable=True),
    sa.Column('app_hold_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('app_hold_source', sa.String(length=32), nullable=True),
    sa.Column('last_full_fetch', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_seen', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['campus_id'], ['campus.id'], ),
    sa.ForeignKeyConstraint(['zone_id'], ['zone.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('identifier')
    )
    op.create_table('override',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('thermostat_id', sa.Integer(), nullable=True),
    sa.Column('zone_id', sa.Integer(), nullable=True),
    sa.Column('group_id', sa.Integer(), nullable=True),
    sa.Column('mode', sa.String(length=16), nullable=False),
    sa.Column('heat', sa.Float(), nullable=True),
    sa.Column('cool', sa.Float(), nullable=True),
    sa.Column('starts_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('ends_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_by', sa.String(length=255), nullable=False),
    sa.Column('reason', sa.String(length=255), nullable=True),
    sa.Column('cancelled', sa.Boolean(), nullable=False),
    sa.ForeignKeyConstraint(['group_id'], ['zone_group.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['thermostat_id'], ['thermostat.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['zone_id'], ['zone.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    op.drop_table('override')
    op.drop_table('thermostat')
    op.drop_table('schedule_block')
    op.drop_table('group_zone')
    with op.batch_alter_table('zone', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_zone_calendar_id'))

    op.drop_table('zone')
    op.drop_table('zone_group')
    op.drop_table('ecobee_credentials')
    op.drop_table('campus')
    op.drop_table('calendar_sync_state')
    with op.batch_alter_table('calendar_event', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_calendar_event_starts_at'))
        batch_op.drop_index(batch_op.f('ix_calendar_event_ends_at'))
        batch_op.drop_index(batch_op.f('ix_calendar_event_calendar_id'))

    op.drop_table('calendar_event')
    with op.batch_alter_table('audit_log', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_audit_log_at'))

    op.drop_table('audit_log')
    op.drop_table('app_user')
    op.drop_table('api_key')
