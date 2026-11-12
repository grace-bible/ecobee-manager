"""Scale settings: comfort profiles, seasonal blocks, per-campus alert webhook, learned lead,
room booking rules.

Revision ID: 0007
Revises: 0006
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "comfort_profile",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("campus_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("occupied_heat", sa.Float(), nullable=False),
        sa.Column("occupied_cool", sa.Float(), nullable=False),
        sa.Column("unoccupied_heat", sa.Float(), nullable=False),
        sa.Column("unoccupied_cool", sa.Float(), nullable=False),
        sa.Column("min_setpoint", sa.Float(), nullable=False),
        sa.Column("max_setpoint", sa.Float(), nullable=False),
        sa.Column("lead_minutes", sa.Integer(), nullable=False),
        sa.Column("tail_minutes", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["campus_id"], ["campus.id"], ondelete="CASCADE",
                                name="fk_comfort_profile_campus"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("zone") as b:
        b.add_column(sa.Column("profile_id", sa.Integer(), nullable=True))
        b.add_column(sa.Column("lead_auto", sa.Boolean(), nullable=False,
                               server_default=sa.false()))
        b.add_column(sa.Column("lead_learned_at", sa.DateTime(timezone=True), nullable=True))
        b.create_foreign_key("fk_zone_profile", "comfort_profile", ["profile_id"], ["id"],
                             ondelete="SET NULL")
    with op.batch_alter_table("schedule_block") as b:
        b.add_column(sa.Column("season_start", sa.String(length=5), nullable=True))
        b.add_column(sa.Column("season_end", sa.String(length=5), nullable=True))
    with op.batch_alter_table("campus") as b:
        b.add_column(sa.Column("chat_webhook", sa.String(length=500), nullable=True))
    with op.batch_alter_table("alert") as b:
        b.add_column(sa.Column("campus_id", sa.Integer(), nullable=True))
    with op.batch_alter_table("room") as b:
        b.add_column(sa.Column("drives_hvac", sa.Boolean(), nullable=False,
                               server_default=sa.true()))
        b.add_column(sa.Column("min_booking_minutes", sa.Integer(), nullable=False,
                               server_default="0"))
        b.add_column(sa.Column("ignore_tentative", sa.Boolean(), nullable=False,
                               server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table("room") as b:
        for c in ("ignore_tentative", "min_booking_minutes", "drives_hvac"):
            b.drop_column(c)
    with op.batch_alter_table("alert") as b:
        b.drop_column("campus_id")
    with op.batch_alter_table("campus") as b:
        b.drop_column("chat_webhook")
    with op.batch_alter_table("schedule_block") as b:
        b.drop_column("season_end")
        b.drop_column("season_start")
    with op.batch_alter_table("zone") as b:
        b.drop_constraint("fk_zone_profile", type_="foreignkey")
        for c in ("lead_learned_at", "lead_auto", "profile_id"):
            b.drop_column(c)
    op.drop_table("comfort_profile")
