"""Learned lead time: how far ahead of a booking a zone should start heating or cooling.

For zones with `lead_auto`, once a night: take the last 30 days of time-to-target (minutes
from a target change until the zone got there), use the 80th percentile plus a 10-minute
margin, rounded up to 5 minutes, kept between 15 and 180. Needs at least 5 measurements;
otherwise the zone keeps its current lead. `learn()` is pure.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db import session_scope
from app.insights import time_to_target
from app.models import AuditLog, Zone, utcnow

MIN_SAMPLES, MARGIN, FLOOR, CEILING = 5, 10, 15, 180


def learn(minutes: list[int]) -> int | None:
    if len(minutes) < MIN_SAMPLES:
        return None
    ordered = sorted(minutes)
    p80 = ordered[min(len(ordered) - 1, math.ceil(0.8 * len(ordered)) - 1)]
    return max(FLOOR, min(CEILING, 5 * math.ceil((p80 + MARGIN) / 5)))


def learn_once(now: datetime | None = None, days: int = 30) -> dict:
    from app.api import room_points  # avoid an import cycle at module load

    now = now or utcnow()
    changed = 0
    with session_scope() as s:
        zones = s.scalars(select(Zone).where(Zone.lead_auto.is_(True)).options(
            selectinload(Zone.thermostats))).all()
        for z in zones:
            runs = time_to_target(room_points(z, now - timedelta(days=days)))
            new = learn([r.minutes for r in runs if r.minutes is not None])
            z.lead_learned_at = now
            if new is not None and new != z.lead_minutes:
                s.add(AuditLog(actor="scheduler", source="system", action="learn_lead",
                               target=z.name, detail={"from": z.lead_minutes, "to": new,
                                                      "samples": len(runs)}))
                z.lead_minutes = new
                changed += 1
    return {"zones": len(zones), "changed": changed}
