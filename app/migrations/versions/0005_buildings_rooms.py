"""Buildings and rooms: campus → building → zone (HVAC) ↔ room (bookable, own calendar)

A zone's single calendar becomes a room linked to that zone, so nothing changes in how
existing zones behave. Overrides and schedule blocks can now target a building.

Revision ID: 0005
Revises: 0004
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "building",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("campus_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("google_building_id", sa.String(length=100), nullable=True),
        sa.ForeignKeyConstraint(["campus_id"], ["campus.id"], name="fk_building_campus"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("campus_id", "name", name="uq_building_campus_name"),
    )
    op.create_table(
        "room",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("campus_id", sa.Integer(), nullable=False),
        sa.Column("building_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("calendar_id", sa.String(length=255), nullable=True),
        sa.Column("capacity", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["building_id"], ["building.id"], ondelete="SET NULL",
                                name="fk_room_building"),
        sa.ForeignKeyConstraint(["campus_id"], ["campus.id"], name="fk_room_campus"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("calendar_id", name="uq_room_calendar_id"),
        sa.UniqueConstraint("campus_id", "name", name="uq_room_campus_name"),
    )
    op.create_table(
        "zone_room",
        sa.Column("zone_id", sa.Integer(), nullable=False),
        sa.Column("room_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["room_id"], ["room.id"], ondelete="CASCADE",
                                name="fk_zone_room_room"),
        sa.ForeignKeyConstraint(["zone_id"], ["zone.id"], ondelete="CASCADE",
                                name="fk_zone_room_zone"),
        sa.PrimaryKeyConstraint("zone_id", "room_id"),
    )
    for table, fk in (("override", "fk_override_building"),
                      ("schedule_block", "fk_schedule_block_building")):
        with op.batch_alter_table(table, schema=None) as b:
            b.add_column(sa.Column("building_id", sa.Integer(), nullable=True))
            b.create_foreign_key(fk, "building", ["building_id"], ["id"], ondelete="CASCADE")
    with op.batch_alter_table("zone", schema=None) as b:
        b.add_column(sa.Column("building_id", sa.Integer(), nullable=True))
        b.create_foreign_key("fk_zone_building", "building", ["building_id"], ["id"],
                             ondelete="SET NULL")

    # Each zone calendar → a room in the same campus, linked to that zone. Zones that shared
    # a calendar share the room.
    conn = op.get_bind()
    rooms: dict[str, int] = {}
    taken: set[tuple[int, str]] = set()
    zones = conn.execute(sa.text(
        "SELECT id, campus_id, name, calendar_id, calendar_name FROM zone "
        "WHERE calendar_id IS NOT NULL AND calendar_id <> '' ORDER BY id")).all()
    for zid, campus_id, zname, cal, cal_name in zones:
        if cal not in rooms:
            name = base = (cal_name if cal_name and cal_name != cal else zname)[:100]
            n = 2
            while (campus_id, name) in taken:
                name, n = f"{base[:95]} ({n})", n + 1
            taken.add((campus_id, name))
            rooms[cal] = conn.execute(sa.text(
                "INSERT INTO room (campus_id, name, calendar_id) VALUES (:c, :n, :cal) "
                "RETURNING id"), {"c": campus_id, "n": name, "cal": cal}).scalar_one()
        conn.execute(sa.text("INSERT INTO zone_room (zone_id, room_id) VALUES (:z, :r)"),
                     {"z": zid, "r": rooms[cal]})

    with op.batch_alter_table("zone", schema=None) as b:
        b.drop_index("ix_zone_calendar_id")
        b.drop_column("calendar_name")
        b.drop_column("calendar_id")


def downgrade() -> None:
    with op.batch_alter_table("zone", schema=None) as b:
        b.add_column(sa.Column("calendar_id", sa.String(length=255), nullable=True))
        b.add_column(sa.Column("calendar_name", sa.String(length=255), nullable=True))
        b.create_index("ix_zone_calendar_id", ["calendar_id"], unique=False)
    # Keep one calendar per zone (the first linked room's); others are lost.
    op.execute(
        "UPDATE zone SET calendar_id = (SELECT r.calendar_id FROM room r JOIN zone_room zr "
        "ON zr.room_id = r.id WHERE zr.zone_id = zone.id AND r.calendar_id IS NOT NULL "
        "ORDER BY r.id LIMIT 1), calendar_name = (SELECT r.name FROM room r JOIN zone_room zr "
        "ON zr.room_id = r.id WHERE zr.zone_id = zone.id AND r.calendar_id IS NOT NULL "
        "ORDER BY r.id LIMIT 1)")
    with op.batch_alter_table("zone", schema=None) as b:
        b.drop_constraint("fk_zone_building", type_="foreignkey")
        b.drop_column("building_id")
    for table, fk in (("schedule_block", "fk_schedule_block_building"),
                      ("override", "fk_override_building")):
        with op.batch_alter_table(table, schema=None) as b:
            b.drop_constraint(fk, type_="foreignkey")
            b.drop_column("building_id")
    op.drop_table("zone_room")
    op.drop_table("room")
    op.drop_table("building")
