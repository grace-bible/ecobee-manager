"""Rollout safety: per-campus/zone live switch, special days (closures / special hours),
campus overrides, all-day booking flag, device program snapshot.

Revision ID: 0006
Revises: 0005
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("campus") as b:
        b.add_column(sa.Column("live", sa.Boolean(), nullable=False, server_default=sa.false()))
    with op.batch_alter_table("zone") as b:
        b.add_column(sa.Column("live", sa.Boolean(), nullable=True))
    with op.batch_alter_table("override") as b:
        b.add_column(sa.Column("campus_id", sa.Integer(), nullable=True))
        b.create_foreign_key("fk_override_campus", "campus", ["campus_id"], ["id"],
                             ondelete="CASCADE")
    with op.batch_alter_table("calendar_event") as b:
        b.add_column(sa.Column("all_day", sa.Boolean(), nullable=False,
                               server_default=sa.false()))
    with op.batch_alter_table("thermostat") as b:
        b.add_column(sa.Column("program", sa.JSON(), nullable=True))
    op.create_table(
        "special_day",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("campus_id", sa.Integer(), nullable=False),
        sa.Column("building_id", sa.Integer(), nullable=True),
        sa.Column("first", sa.Date(), nullable=False),
        sa.Column("last", sa.Date(), nullable=False),
        sa.Column("closed", sa.Boolean(), nullable=False),
        sa.Column("start", sa.Time(), nullable=True),
        sa.Column("end", sa.Time(), nullable=True),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("heat", sa.Float(), nullable=True),
        sa.Column("cool", sa.Float(), nullable=True),
        sa.Column("closed_mode", sa.String(length=16), nullable=False),
        sa.Column("ignore_bookings", sa.Boolean(), nullable=False),
        sa.Column("label", sa.String(length=100), nullable=True),
        sa.ForeignKeyConstraint(["campus_id"], ["campus.id"], ondelete="CASCADE",
                                name="fk_special_day_campus"),
        sa.ForeignKeyConstraint(["building_id"], ["building.id"], ondelete="CASCADE",
                                name="fk_special_day_building"),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("special_day")
    with op.batch_alter_table("thermostat") as b:
        b.drop_column("program")
    with op.batch_alter_table("calendar_event") as b:
        b.drop_column("all_day")
    with op.batch_alter_table("override") as b:
        b.drop_constraint("fk_override_campus", type_="foreignkey")
        b.drop_column("campus_id")
    with op.batch_alter_table("zone") as b:
        b.drop_column("live")
    with op.batch_alter_table("campus") as b:
        b.drop_column("live")
