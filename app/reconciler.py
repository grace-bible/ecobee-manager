"""Desired (resolver) vs. what we last sent → batched ecobee holds.

Write policy:
- Re-send only when the target changes, or our hold is within REFRESH_BEFORE of expiring
  and a new hold would end later than the one already sent.
  A change made at the wall therefore stands until the next scheduled transition (or
  the hold refresh, ≤ HOLD_HORIZON later) instead of being fought every minute.
- Every hold ends HOLD_OVERLAP after the resolver's next change, capped at HOLD_HORIZON, so
  a dead app leaves thermostats on their own program within hours, never stuck. The overlap
  covers the up-to-a-minute until the next reconcile sends the new target; without it the
  thermostat would fall back to its own program at every transition.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import or_, select, text
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.db import engine, session_scope
from app.ecobee.backend import BackendError
from app.models import (
    AuditLog,
    CalendarEvent,
    Override,
    ScheduleBlock,
    Thermostat,
    Zone,
    aware,
    utcnow,
)
from app.resolver import BlockIn, Desired, EventIn, OverrideIn, ZoneCfg, next_change, resolve
from app.services import get_backend

log = logging.getLogger(__name__)
REFRESH_BEFORE = timedelta(minutes=15)
HOLD_OVERLAP = timedelta(minutes=5)
LOCK_ID = 0xEC0BEE  # Postgres advisory lock; a second replica just skips


def zone_cfg(z: Zone) -> ZoneCfg:
    return ZoneCfg(z.occupied_heat, z.occupied_cool, z.unoccupied_heat, z.unoccupied_cool,
                   z.min_setpoint, z.max_setpoint, z.lead_minutes, z.tail_minutes,
                   z.campus.timezone if z.campus else get_settings().default_timezone)


def gather_inputs(s, z: Zone, now: datetime, horizon: timedelta):
    """Everything the resolver needs for one zone, as plain dataclasses."""
    group_ids = [g.id for g in z.groups]
    ovs = s.scalars(select(Override).where(
        Override.cancelled.is_(False), Override.ends_at > now,
        Override.starts_at < now + horizon)).all()
    zone_ovs, therm_ovs = [], defaultdict(list)
    for o in ovs:
        level = 0 if o.thermostat_id else 1 if o.zone_id else 2 if o.group_id else 3
        oi = OverrideIn(level, o.mode, o.heat, o.cool, aware(o.starts_at), aware(o.ends_at), o.id)
        if (o.zone_id == z.id or (o.group_id and o.group_id in group_ids)
                or (o.building_id and o.building_id == z.building_id)):
            zone_ovs.append(oi)
        elif o.thermostat_id:
            therm_ovs[o.thermostat_id].append(oi)

    # Bookings in any room the zone serves. One event booked in several of its rooms counts
    # once (resource calendars share the organizer's event id).
    events, seen = [], set()
    room_of = {r.calendar_id: r.name for r in z.rooms if r.calendar_id}
    if room_of:
        lead, tail = timedelta(minutes=z.lead_minutes), timedelta(minutes=z.tail_minutes)
        for e in s.scalars(select(CalendarEvent).where(
                CalendarEvent.calendar_id.in_(list(room_of)),
                CalendarEvent.starts_at < now + horizon + lead,
                CalendarEvent.ends_at > now - tail).order_by(CalendarEvent.starts_at)):
            if e.event_id in seen:
                continue
            seen.add(e.event_id)
            events.append(EventIn(aware(e.starts_at), aware(e.ends_at), e.title or "",
                                  e.hvac_tag, e.event_id, room_of.get(e.calendar_id)))

    owner = [ScheduleBlock.zone_id == z.id]
    if group_ids:
        owner.append(ScheduleBlock.group_id.in_(group_ids))
    if z.building_id:
        owner.append(ScheduleBlock.building_id == z.building_id)
    blocks = [BlockIn(b.weekdays, b.start, b.end, b.mode, b.heat, b.cool,
                      0 if b.zone_id else 1 if b.group_id else 2, b.id)
              for b in s.scalars(select(ScheduleBlock).where(or_(*owner)))]
    return zone_ovs, therm_ovs, events, blocks


def plan(now: datetime | None = None) -> list[dict]:
    """Compute what each managed thermostat should get. Pure read; no writes."""
    st = get_settings()
    now = now or utcnow()
    horizon = timedelta(minutes=st.hold_horizon_minutes)
    out = []
    with session_scope() as s:
        zones = s.scalars(select(Zone).where(Zone.enabled.is_(True)).options(
            selectinload(Zone.thermostats), selectinload(Zone.groups),
            selectinload(Zone.campus), selectinload(Zone.rooms))).all()
        for z in zones:
            cfg = zone_cfg(z)
            zone_ovs, therm_ovs, events, blocks = gather_inputs(s, z, now, horizon)
            for t in z.thermostats:
                ovs = zone_ovs + therm_ovs.get(t.id, [])
                d: Desired = resolve(cfg, now, ovs, events, blocks, st.min_deadband_f)
                until = next_change(cfg, now, ovs, events, blocks, horizon=horizon)
                hold_until = min(until + HOLD_OVERLAP, now + horizon)
                sent_until = aware(t.app_hold_until)
                needs = (
                    (t.app_hold_heat, t.app_hold_cool) != d.key
                    or sent_until is None
                    # Refresh a hold that is about to lapse, but only if re-sending extends
                    # it. A hold that already ends at the next change is left alone, or
                    # the last REFRESH_BEFORE before every transition re-sends each minute.
                    or (sent_until - now < REFRESH_BEFORE
                        and hold_until.replace(second=0, microsecond=0) > sent_until)
                    # Only "sent" in dry run → really send once dry run is turned off.
                    or (not st.dry_run and (t.app_hold_source or "").startswith("dry:"))
                )
                out.append({
                    "thermostat_id": t.id, "identifier": t.identifier, "name": t.name,
                    "zone": z.name, "connected": t.connected, "tz": cfg.timezone,
                    "heat": d.heat, "cool": d.cool, "source": d.source, "ref": d.ref,
                    "intent": d.intent, "notes": d.notes, "until": until, "hold_until": hold_until,
                    "needs_write": needs,
                })
    return out


def _try_lock(conn) -> bool:
    if engine.dialect.name != "postgresql":
        return True
    return bool(conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": LOCK_ID}).scalar())


def reconcile_once() -> dict:
    with engine.connect() as conn:
        if not _try_lock(conn):
            return {"skipped": "another replica holds the lock"}
        try:
            return _reconcile()
        finally:
            if engine.dialect.name == "postgresql":
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LOCK_ID})


def _reconcile() -> dict:
    backend = get_backend()
    now = utcnow()
    items = [p for p in plan(now) if p["needs_write"] and p["connected"]]
    # Same target + same end + same timezone → one API call for all of them.
    batches: dict[tuple, list[dict]] = defaultdict(list)
    for p in items:
        until = p["hold_until"].replace(second=0, microsecond=0)
        batches[(p["heat"], p["cool"], until, p["tz"])].append(p)

    sent, failed = 0, 0
    for (heat, cool, until, tz), group in batches.items():
        ids = [p["identifier"] for p in group]
        try:
            backend.set_hold(ids, heat, cool, until, tz)
        except BackendError as err:
            failed += len(ids)
            log.warning("set_hold failed for %s: %s", ids, err)
            with session_scope() as s:
                s.add(AuditLog(actor="scheduler", source="system", action="set_hold_failed",
                               target=",".join(ids), detail={"error": str(err)}))
            continue
        sent += len(ids)
        dry = get_settings().dry_run
        with session_scope() as s:
            for p in group:
                t = s.get(Thermostat, p["thermostat_id"])
                t.app_hold_heat, t.app_hold_cool = heat, cool
                t.app_hold_until = until
                t.app_hold_source = ("dry:" if dry else "") + p["source"]
            s.add(AuditLog(
                actor="scheduler", source=group[0]["source"],
                action="set_hold" + (" (dry run)" if dry else ""),
                target=", ".join(p["name"] for p in group),
                detail={"heat": heat, "cool": cool, "until": until.isoformat(),
                        "refs": sorted({p["ref"] for p in group if p["ref"]}),
                        "notes": sorted({n for p in group for n in p["notes"]})}))
    return {"planned": len(items), "sent": sent, "failed": failed, "batches": len(batches)}
