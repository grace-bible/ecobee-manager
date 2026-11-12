"""What the 5-minute history says about a room. Pure functions, no I/O (like the resolver).

- `merge_room`: several thermostats in one room → one series.
- `time_to_target`: each time the target band tightened (heating up or cooling down to a
  new target), how long the room took to get there.
- `downsample`: average into wider steps for long chart ranges.
- `daily_runtime` / `hourly_runtime`: heating/cooling hours per local day or hour.
- `occupancy`: hours conditioned for nobody, and hours occupied at setback (needs a
  thermostat with an occupancy sensor).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from statistics import median
from zoneinfo import ZoneInfo

INTERVAL_H = 5 / 60


@dataclass(frozen=True)
class Point:
    at: datetime
    temp: float | None
    heat_sp: float | None
    cool_sp: float | None
    outdoor: float | None = None
    heat_s: float = 0  # seconds of heating in the 5-minute interval
    cool_s: float = 0
    occupied: bool | None = None


@dataclass(frozen=True)
class TargetRun:
    kind: str  # "heat" | "cool"
    started: datetime
    target: float
    start_temp: float
    minutes: int | None  # None = didn't get there within give_up


def merge_room(series: list[list[Point]]) -> list[Point]:
    """Average temperature/setpoints/runtime across thermostats at each timestamp; a room
    is occupied if any of its sensors says so."""
    by_at: dict[datetime, list[Point]] = defaultdict(list)
    for s in series:
        for p in s:
            by_at[p.at].append(p)

    def avg(vals):
        vals = [v for v in vals if v is not None]
        return round(sum(vals) / len(vals), 1) if vals else None

    out = []
    for at in sorted(by_at):
        ps = by_at[at]
        occ = [p.occupied for p in ps if p.occupied is not None]
        out.append(Point(at, avg(p.temp for p in ps), avg(p.heat_sp for p in ps),
                         avg(p.cool_sp for p in ps), avg(p.outdoor for p in ps),
                         avg(p.heat_s for p in ps) or 0, avg(p.cool_s for p in ps) or 0,
                         any(occ) if occ else None))
    return out


def time_to_target(points: list[Point], min_step: float = 2.0, tolerance: float = 0.5,
            give_up: timedelta = timedelta(hours=3)) -> list[TargetRun]:
    """Each time heat rose (or cool fell) by ≥ min_step while the room was outside the new
    band: minutes until the room came within `tolerance` of it."""
    out = []
    for i in range(1, len(points)):
        prev, p = points[i - 1], points[i]
        if p.temp is None:
            continue
        for kind, before, after in (("heat", prev.heat_sp, p.heat_sp),
                                    ("cool", prev.cool_sp, p.cool_sp)):
            if before is None or after is None:
                continue
            rose = after - before if kind == "heat" else before - after
            outside = (p.temp < after - tolerance) if kind == "heat" else (
                p.temp > after + tolerance)
            if rose < min_step or not outside:
                continue
            minutes = None
            for q in points[i:]:
                if q.at - p.at > give_up:
                    break
                sp = q.heat_sp if kind == "heat" else q.cool_sp
                if sp != after:  # target changed again before we got there
                    break
                if q.temp is not None and (q.temp >= after - tolerance if kind == "heat"
                                           else q.temp <= after + tolerance):
                    minutes = int((q.at - p.at).total_seconds() // 60)
                    break
            out.append(TargetRun(kind, p.at, after, p.temp, minutes))
    return out


def time_to_target_summary(ws: list[TargetRun]) -> dict:
    done = [w.minutes for w in ws if w.minutes is not None]
    return {"count": len(ws), "reached": len(done), "missed": len(ws) - len(done),
            "median": int(median(done)) if done else None,
            "slowest": max(done) if done else None}


def daily_runtime(points: list[Point], tz: str) -> list[tuple[date, float, float]]:
    """[(local day, heating hours, cooling hours)] oldest first."""
    zone = ZoneInfo(tz)
    days: dict[date, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for p in points:
        d = days[p.at.astimezone(zone).date()]
        d[0] += p.heat_s / 3600
        d[1] += p.cool_s / 3600
    return [(d, round(h, 2), round(c, 2)) for d, (h, c) in sorted(days.items())]


def hourly_runtime(points: list[Point], tz: str) -> list[tuple[datetime, float, float]]:
    """[(local hour start, heating hours, cooling hours)] oldest first, for short ranges."""
    zone = ZoneInfo(tz)
    hours: dict[datetime, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for p in points:
        h = hours[p.at.astimezone(zone).replace(minute=0, second=0, microsecond=0)]
        h[0] += p.heat_s / 3600
        h[1] += p.cool_s / 3600
    return [(h, round(a, 2), round(b, 2)) for h, (a, b) in sorted(hours.items())]


def occupancy(points: list[Point], setback_heat: float, setback_cool: float,
              margin: float = 1.0) -> dict:
    """Conditioned = target band tighter than the room's setback. Hours conditioned while
    the sensor saw nobody, and hours someone was there while it sat at setback."""
    known = [p for p in points if p.occupied is not None and p.heat_sp is not None
             and p.cool_sp is not None]
    # Thermostats without a sensor report "unoccupied" forever rather than nothing, so only
    # trust the column once it has seen someone.
    if not any(p.occupied for p in known):
        return {"has_data": False, "conditioned_empty_h": 0.0, "occupied_setback_h": 0.0}

    def conditioned(p: Point) -> bool:
        return p.heat_sp > setback_heat + margin or p.cool_sp < setback_cool - margin

    empty = sum(INTERVAL_H for p in known if conditioned(p) and not p.occupied)
    missed = sum(INTERVAL_H for p in known if not conditioned(p) and p.occupied)
    return {"has_data": True, "conditioned_empty_h": round(empty, 2),
            "occupied_setback_h": round(missed, 2)}


def downsample(points: list[Point], step: timedelta) -> list[Point]:
    """Average into `step`-wide buckets (5-minute data over 30 days is 8,640 points)."""
    if step <= timedelta(minutes=5) or not points:
        return points
    secs = int(step.total_seconds())
    buckets: dict[datetime, list[Point]] = defaultdict(list)
    for p in points:
        ts = int(p.at.timestamp())
        buckets[datetime.fromtimestamp(ts - ts % secs, tz=p.at.tzinfo)].append(p)
    # Same averaging as merging thermostats; occupied if anyone was there in the bucket.
    return [Point(at, *_avg_fields(ps)) for at, ps in sorted(buckets.items())]


def _avg_fields(ps: list[Point]):
    def avg(vals):
        vals = [v for v in vals if v is not None]
        return round(sum(vals) / len(vals), 1) if vals else None

    occ = [p.occupied for p in ps if p.occupied is not None]
    return (avg(p.temp for p in ps), avg(p.heat_sp for p in ps), avg(p.cool_sp for p in ps),
            avg(p.outdoor for p in ps), avg(p.heat_s for p in ps) or 0,
            avg(p.cool_s for p in ps) or 0, any(occ) if occ else None)

