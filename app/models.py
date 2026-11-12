"""Database tables. See docs/SPEC.md §4."""

from datetime import UTC, datetime, time

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


def aware(dt: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; everything here is stored as UTC."""
    if dt is None or dt.tzinfo:
        return dt
    return dt.replace(tzinfo=UTC)


class Campus(Base):
    __tablename__ = "campus"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    timezone: Mapped[str] = mapped_column(String(64), default="America/Chicago")

    zones: Mapped[list["Zone"]] = relationship(back_populates="campus")
    thermostats: Mapped[list["Thermostat"]] = relationship(back_populates="campus")
    buildings: Mapped[list["Building"]] = relationship(back_populates="campus",
                                                       order_by="Building.name")
    rooms: Mapped[list["Room"]] = relationship(back_populates="campus", order_by="Room.name")


class Building(Base):
    """A building on a campus. Zones and rooms live in one; it can be overridden or given a
    weekly schedule as a whole (below zone and group settings)."""

    __tablename__ = "building"
    __table_args__ = (UniqueConstraint("campus_id", "name"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    campus_id: Mapped[int] = mapped_column(ForeignKey("campus.id"))
    name: Mapped[str] = mapped_column(String(100))
    google_building_id: Mapped[str | None] = mapped_column(String(100))  # Workspace buildingId

    campus: Mapped[Campus] = relationship(back_populates="buildings")
    zones: Mapped[list["Zone"]] = relationship(back_populates="building", order_by="Zone.name")
    rooms: Mapped[list["Room"]] = relationship(back_populates="building", order_by="Room.name")


class Thermostat(Base):
    """Last-known state from ecobee. Temperatures in °F (ecobee reports tenths)."""

    __tablename__ = "thermostat"
    id: Mapped[int] = mapped_column(primary_key=True)
    identifier: Mapped[str] = mapped_column(String(32), unique=True)  # ecobee serial
    name: Mapped[str] = mapped_column(String(100))
    model: Mapped[str | None] = mapped_column(String(64))
    campus_id: Mapped[int | None] = mapped_column(ForeignKey("campus.id"))
    zone_id: Mapped[int | None] = mapped_column(ForeignKey("zone.id"))

    connected: Mapped[bool] = mapped_column(Boolean, default=False)
    current_temp: Mapped[float | None] = mapped_column(Float)
    humidity: Mapped[int | None] = mapped_column(Integer)
    heat_setpoint: Mapped[float | None] = mapped_column(Float)
    cool_setpoint: Mapped[float | None] = mapped_column(Float)
    hvac_mode: Mapped[str | None] = mapped_column(String(16))  # heat/cool/auto/off/auxHeatOnly
    equipment_running: Mapped[str | None] = mapped_column(String(200))
    active_hold: Mapped[dict | None] = mapped_column(JSON)  # current hold event, if any
    revision: Mapped[str | None] = mapped_column(String(64))  # thermostatSummary revisions
    # What the reconciler last sent, so it only re-sends on change or before expiry.
    app_hold_heat: Mapped[float | None] = mapped_column(Float)
    app_hold_cool: Mapped[float | None] = mapped_column(Float)
    app_hold_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    app_hold_source: Mapped[str | None] = mapped_column(String(32))
    last_full_fetch: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    timezone: Mapped[str | None] = mapped_column(String(64))  # thermostat's own, from ecobee
    ecobee_alerts: Mapped[list | None] = mapped_column(JSON)  # active alerts on the device

    campus: Mapped[Campus | None] = relationship(back_populates="thermostats")
    zone: Mapped["Zone | None"] = relationship(back_populates="thermostats")


zone_room = Table(
    "zone_room",
    Base.metadata,
    Column("zone_id", ForeignKey("zone.id", ondelete="CASCADE"), primary_key=True),
    Column("room_id", ForeignKey("room.id", ondelete="CASCADE"), primary_key=True),
)


class Room(Base):
    """A bookable space with its own Google room calendar. Served by one or more zones
    (usually one); a zone is "booked" when any of its rooms is."""

    __tablename__ = "room"
    __table_args__ = (UniqueConstraint("campus_id", "name"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    campus_id: Mapped[int] = mapped_column(ForeignKey("campus.id"))
    building_id: Mapped[int | None] = mapped_column(ForeignKey("building.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(String(100))
    # Google Workspace resource calendar (resourceEmail, c_…@resource.calendar.google.com)
    calendar_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    capacity: Mapped[int | None] = mapped_column(Integer)

    campus: Mapped[Campus] = relationship(back_populates="rooms")
    building: Mapped["Building | None"] = relationship(back_populates="rooms")
    zones: Mapped[list["Zone"]] = relationship(secondary=zone_room, back_populates="rooms")


group_zone = Table(
    "group_zone",
    Base.metadata,
    Column("group_id", ForeignKey("zone_group.id", ondelete="CASCADE"), primary_key=True),
    Column("zone_id", ForeignKey("zone.id", ondelete="CASCADE"), primary_key=True),
)


class Zone(Base):
    """What the HVAC controls: one or more thermostats that move together, with their own
    comfort settings and schedule. Serves one or more rooms, whose calendars drive it."""

    __tablename__ = "zone"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    campus_id: Mapped[int] = mapped_column(ForeignKey("campus.id"))
    building_id: Mapped[int | None] = mapped_column(ForeignKey("building.id", ondelete="SET NULL"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # Bookings in any of the zone's rooms start conditioning this early / keep it this long.
    lead_minutes: Mapped[int] = mapped_column(Integer, default=45)
    tail_minutes: Mapped[int] = mapped_column(Integer, default=15)

    occupied_heat: Mapped[float] = mapped_column(Float, default=68)
    occupied_cool: Mapped[float] = mapped_column(Float, default=72)
    unoccupied_heat: Mapped[float] = mapped_column(Float, default=60)
    unoccupied_cool: Mapped[float] = mapped_column(Float, default=82)
    min_setpoint: Mapped[float] = mapped_column(Float, default=60)
    max_setpoint: Mapped[float] = mapped_column(Float, default=85)

    campus: Mapped[Campus] = relationship(back_populates="zones")
    thermostats: Mapped[list[Thermostat]] = relationship(back_populates="zone")
    groups: Mapped[list["ZoneGroup"]] = relationship(secondary=group_zone, back_populates="zones")
    building: Mapped[Building | None] = relationship(back_populates="zones")
    rooms: Mapped[list[Room]] = relationship(secondary=zone_room, back_populates="zones",
                                             order_by="Room.name")

    __table_args__ = (UniqueConstraint("campus_id", "name"),)

    @property
    def calendar_ids(self) -> list[str]:
        return sorted({r.calendar_id for r in self.rooms if r.calendar_id})


class ZoneGroup(Base):
    __tablename__ = "zone_group"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    zones: Mapped[list[Zone]] = relationship(secondary=group_zone, back_populates="groups")


class ScheduleBlock(Base):
    """Weekly recurring occupancy for a zone, group or building (exactly one set)."""

    __tablename__ = "schedule_block"
    id: Mapped[int] = mapped_column(primary_key=True)
    zone_id: Mapped[int | None] = mapped_column(ForeignKey("zone.id", ondelete="CASCADE"))
    group_id: Mapped[int | None] = mapped_column(ForeignKey("zone_group.id", ondelete="CASCADE"))
    building_id: Mapped[int | None] = mapped_column(ForeignKey("building.id", ondelete="CASCADE"))
    weekdays: Mapped[int] = mapped_column(Integer)  # bitmask, Mon=1 … Sun=64
    start: Mapped[time] = mapped_column(Time)
    end: Mapped[time] = mapped_column(Time)
    mode: Mapped[str] = mapped_column(String(16), default="occupied")  # occupied/unoccupied/off/custom
    heat: Mapped[float | None] = mapped_column(Float)  # only for mode=custom
    cool: Mapped[float | None] = mapped_column(Float)
    label: Mapped[str | None] = mapped_column(String(100))


class Override(Base):
    """Manual, time-bounded change from the UI or API. Exactly one target set."""

    __tablename__ = "override"
    id: Mapped[int] = mapped_column(primary_key=True)
    thermostat_id: Mapped[int | None] = mapped_column(ForeignKey("thermostat.id", ondelete="CASCADE"))
    zone_id: Mapped[int | None] = mapped_column(ForeignKey("zone.id", ondelete="CASCADE"))
    group_id: Mapped[int | None] = mapped_column(ForeignKey("zone_group.id", ondelete="CASCADE"))
    building_id: Mapped[int | None] = mapped_column(ForeignKey("building.id", ondelete="CASCADE"))
    mode: Mapped[str] = mapped_column(String(16), default="custom")  # occupied/unoccupied/off/custom
    heat: Mapped[float | None] = mapped_column(Float)
    cool: Mapped[float | None] = mapped_column(Float)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(String(255))
    reason: Mapped[str | None] = mapped_column(String(255))
    cancelled: Mapped[bool] = mapped_column(Boolean, default=False)


class CalendarEvent(Base):
    __tablename__ = "calendar_event"
    id: Mapped[int] = mapped_column(primary_key=True)
    calendar_id: Mapped[str] = mapped_column(String(255), index=True)
    event_id: Mapped[str] = mapped_column(String(255))
    title: Mapped[str | None] = mapped_column(String(500))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(16), default="confirmed")
    hvac_tag: Mapped[dict | None] = mapped_column(JSON)  # parsed #nohvac / #hvac:… tags

    __table_args__ = (UniqueConstraint("calendar_id", "event_id"),)


class CalendarSyncState(Base):
    __tablename__ = "calendar_sync_state"
    calendar_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    sync_token: Mapped[str | None] = mapped_column(Text)
    last_success: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(255))  # email, "scheduler", or api key name
    source: Mapped[str] = mapped_column(String(16))  # ui/api/calendar/schedule/default/system
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str] = mapped_column(String(255))
    detail: Mapped[dict | None] = mapped_column(JSON)


class AppUser(Base):
    __tablename__ = "app_user"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    role: Mapped[str] = mapped_column(String(16), default="viewer")  # admin/campus_manager/viewer
    campus_ids: Mapped[list | None] = mapped_column(JSON)  # scope for campus_manager
    last_login: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ApiKey(Base):
    __tablename__ = "api_key"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    key_hash: Mapped[str] = mapped_column(String(128), unique=True)
    # Same roles as people: a write key is a campus manager for named campuses unless it
    # was explicitly made an admin.
    role: Mapped[str] = mapped_column(String(16), default="viewer")  # admin/campus_manager/viewer
    campus_ids: Mapped[list | None] = mapped_column(JSON)  # scope for campus_manager
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EcobeeCredentials(Base):
    """Single row. Tokens are Fernet-encrypted with APP_SECRET_KEY."""

    __tablename__ = "ecobee_credentials"
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    refresh_token_enc: Mapped[str | None] = mapped_column(Text)
    access_token_enc: Mapped[str | None] = mapped_column(Text)
    last_login: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class Alert(Base):
    """One row per incident (docs/SPEC.md §9). Opened once, resolved once → two messages."""

    __tablename__ = "alert"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(255), index=True)  # e.g. "offline:511800000001"
    kind: Mapped[str] = mapped_column(String(32))  # offline/setpoint/ecobee_auth/calendar
    message: Mapped[str] = mapped_column(Text)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    notified: Mapped[bool] = mapped_column(Boolean, default=False)


class RuntimeSample(Base):
    """ecobee runtime report, one row per thermostat per 5 minutes (history and charts)."""

    __tablename__ = "runtime_sample"
    __table_args__ = (UniqueConstraint("thermostat_id", "at"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    thermostat_id: Mapped[int] = mapped_column(
        ForeignKey("thermostat.id", ondelete="CASCADE"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    temp: Mapped[float | None] = mapped_column(Float)
    humidity: Mapped[int | None] = mapped_column(Integer)
    heat_sp: Mapped[float | None] = mapped_column(Float)
    cool_sp: Mapped[float | None] = mapped_column(Float)
    outdoor: Mapped[float | None] = mapped_column(Float)
    heat_s: Mapped[int] = mapped_column(Integer, default=0)
    cool_s: Mapped[int] = mapped_column(Integer, default=0)
    fan_s: Mapped[int] = mapped_column(Integer, default=0)
    occupied: Mapped[bool | None] = mapped_column(Boolean)

