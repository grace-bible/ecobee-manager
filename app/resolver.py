"""Decide what each zone should be doing right now. Pure functions, no I/O.

Priority (docs/SPEC.md §5):
  1. manual override   thermostat > zone > group, newest first within a level
  2. calendar event    on the zone's resource calendar, widened by lead/tail
  3. schedule block    zone blocks, then group blocks
  4. default           zone's unoccupied setpoints
Then guardrails clamp to [min, max] and enforce a heat/cool deadband.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

OCCUPIED, UNOCCUPIED, OFF, CUSTOM = "occupied", "unoccupied", "off", "custom"
WEEKDAY_BITS = {i: 1 << i for i in range(7)}  # Mon=0 → 1 … Sun=6 → 64

_TAG_NOHVAC = re.compile(r"#no-?hvac\b", re.IGNORECASE)
_TAG_SET = re.compile(r"#hvac:(heat|cool)=(\d{2}(?:\.\d)?)", re.IGNORECASE)


@dataclass(frozen=True)
class ZoneCfg:
    occupied_heat: float
    occupied_cool: float
    unoccupied_heat: float
    unoccupied_cool: float
    min_setpoint: float
    max_setpoint: float
    lead_minutes: int = 45
    tail_minutes: int = 15
    timezone: str = "America/Chicago"


@dataclass(frozen=True)
class OverrideIn:
    level: int  # 0 = thermostat, 1 = zone, 2 = group (lower wins)
    mode: str
    heat: float | None
    cool: float | None
    starts_at: datetime
    ends_at: datetime
    id: int | None = None


@dataclass(frozen=True)
class EventIn:
    starts_at: datetime
    ends_at: datetime
    title: str = ""
    tag: dict | None = None  # output of parse_hvac_tags
    id: str | None = None


@dataclass(frozen=True)
class BlockIn:
    weekdays: int
    start: time
    end: time
    mode: str = OCCUPIED
    heat: float | None = None
    cool: float | None = None
    is_group: bool = False
    id: int | None = None


@dataclass
class Desired:
    """Always a self-expiring hold. "off" is expressed as the widest band the zone's
    guardrails allow, never as hvacMode=off, which wouldn't expire if the app died."""

    heat: float
    cool: float
    source: str  # override/calendar/schedule/default
    ref: str | None = None
    intent: str = OCCUPIED  # occupied/unoccupied/off/custom, for display
    notes: list[str] = field(default_factory=list)

    @property
    def key(self) -> tuple[float, float]:
        return (self.heat, self.cool)


def parse_hvac_tags(*texts: str | None) -> dict | None:
    """Read `#nohvac`, `#hvac:heat=68`, `#hvac:cool=72` from event title/description."""
    blob = " ".join(t for t in texts if t)
    if not blob:
        return None
    if _TAG_NOHVAC.search(blob):
        return {"ignore": True}
    found = {k.lower(): float(v) for k, v in _TAG_SET.findall(blob)}
    return found or None


def _setpoints_for(mode: str, z: ZoneCfg, heat: float | None, cool: float | None):
    if mode == OCCUPIED:
        return z.occupied_heat, z.occupied_cool
    if mode == UNOCCUPIED:
        return z.unoccupied_heat, z.unoccupied_cool
    if mode == OFF:
        return z.min_setpoint, z.max_setpoint
    if mode == CUSTOM:
        return (heat if heat is not None else z.occupied_heat,
                cool if cool is not None else z.occupied_cool)
    raise ValueError(f"unknown mode {mode!r}")


def _make(mode: str, z: ZoneCfg, heat, cool, source: str, ref: str | None, deadband: float):
    h, c = _setpoints_for(mode, z, heat, cool)
    return apply_guardrails(Desired(h, c, source, ref, intent=mode), z, deadband)


def apply_guardrails(d: Desired, z: ZoneCfg, deadband: float = 4.0) -> Desired:
    heat = min(max(d.heat, z.min_setpoint), z.max_setpoint)
    cool = min(max(d.cool, z.min_setpoint), z.max_setpoint)
    if (heat, cool) != (d.heat, d.cool):
        d.notes.append(f"clamped to {z.min_setpoint:g}–{z.max_setpoint:g}°F")
    if cool - heat < deadband:
        # Keep the requested midpoint, widen symmetrically, stay inside limits.
        mid = (heat + cool) / 2
        heat, cool = mid - deadband / 2, mid + deadband / 2
        if heat < z.min_setpoint:
            heat, cool = z.min_setpoint, z.min_setpoint + deadband
        if cool > z.max_setpoint:
            heat, cool = z.max_setpoint - deadband, z.max_setpoint
        d.notes.append(f"widened to {deadband:g}°F deadband")
    d.heat, d.cool = round(heat, 1), round(cool, 1)
    return d


def _block_active(b: BlockIn, local: datetime) -> bool:
    t = local.time()
    today = bool(b.weekdays & WEEKDAY_BITS[local.weekday()])
    if b.end > b.start:
        return today and b.start <= t < b.end
    # Overnight block (e.g. 22:00–02:00): tonight's part, or the tail of yesterday's.
    yesterday = bool(b.weekdays & WEEKDAY_BITS[(local.weekday() - 1) % 7])
    return (today and t >= b.start) or (yesterday and t < b.end)


def resolve(
    zone: ZoneCfg,
    now: datetime,
    overrides: list[OverrideIn] = (),
    events: list[EventIn] = (),
    blocks: list[BlockIn] = (),
    deadband: float = 4.0,
) -> Desired:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    # 1. Overrides
    live = [o for o in overrides if o.starts_at <= now < o.ends_at]
    if live:
        o = min(live, key=lambda o: (o.level, -o.starts_at.timestamp()))
        return _make(o.mode, zone, o.heat, o.cool, "override", f"override:{o.id}", deadband)

    # 2. Calendar
    lead, tail = timedelta(minutes=zone.lead_minutes), timedelta(minutes=zone.tail_minutes)
    hits = [e for e in events
            if not (e.tag or {}).get("ignore") and e.starts_at - lead <= now < e.ends_at + tail]
    if hits:
        e = min(hits, key=lambda e: e.starts_at)
        tag = e.tag or {}
        # Several overlapping events: honor the most demanding custom setpoints.
        heat = max([h.tag["heat"] for h in hits if h.tag and "heat" in h.tag], default=None)
        cool = min([h.tag["cool"] for h in hits if h.tag and "cool" in h.tag], default=None)
        mode = CUSTOM if (heat is not None or cool is not None) else OCCUPIED
        d = _make(mode, zone, heat, cool, "calendar", f"event:{e.id}", deadband)
        if tag:
            d.notes.append("event tag applied")
        return d

    # 3. Schedule (zone blocks beat group blocks)
    local = now.astimezone(ZoneInfo(zone.timezone))
    active = [b for b in blocks if _block_active(b, local)]
    if active:
        b = min(active, key=lambda b: b.is_group)
        return _make(b.mode, zone, b.heat, b.cool, "schedule", f"block:{b.id}", deadband)

    # 4. Default
    return _make(UNOCCUPIED, zone, None, None, "default", None, deadband)


def next_change(
    zone: ZoneCfg,
    now: datetime,
    overrides: list[OverrideIn] = (),
    events: list[EventIn] = (),
    blocks: list[BlockIn] = (),
    horizon: timedelta = timedelta(hours=4),
    step: timedelta = timedelta(minutes=5),
) -> datetime:
    """When does the resolved target next change? Used as the hold end time, so a
    hold never outlives the reason for it (capped at `horizon`)."""
    current = resolve(zone, now, overrides, events, blocks)
    end = now + horizon
    prev, t = now, now + step
    while t < end:
        if resolve(zone, t, overrides, events, blocks).key != current.key:
            # Coarse step found the change; pin it to the first whole minute it holds.
            # Always a whole minute (up to t rounded up), so the answer is the same however
            # far `now` has moved — the reconciler compares it with the hold it sent.
            m = (prev + timedelta(minutes=1)).replace(second=0, microsecond=0)
            last = t.replace(second=0, microsecond=0)
            if last < t:
                last += timedelta(minutes=1)
            while m <= last:
                if resolve(zone, m, overrides, events, blocks).key != current.key:
                    return m
                m += timedelta(minutes=1)
            return last
        prev, t = t, t + step
    return end
