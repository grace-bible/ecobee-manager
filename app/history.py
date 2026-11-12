"""ecobee runtime report → `runtime_sample` (5-minute history for charts and insights).

ecobee uploads interval data about every 15 minutes, so this runs on that cadence. Each
run re-reads from a little before the newest stored sample (late intervals get filled in)
and backfills HISTORY_BACKFILL_DAYS for thermostats it hasn't seen yet. Old rows are
pruned after HISTORY_KEEP_DAYS.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from sqlalchemy import delete, func, select

from app.config import get_settings
from app.db import session_scope
from app.ecobee.backend import AuthRequired, BackendError, RuntimeRow
from app.models import RuntimeSample, Thermostat, aware, utcnow
from app.services import get_backend, record_auth_error

log = logging.getLogger(__name__)
REREAD = timedelta(hours=2)  # intervals can arrive late; re-read this much each time


def sync_once(now: datetime | None = None) -> dict:
    st = get_settings()
    now = now or utcnow()
    with session_scope() as s:
        therms = {t.id: (t.identifier, t.timezone or st.default_timezone)
                  for t in s.scalars(select(Thermostat))}
        newest = dict(s.execute(select(RuntimeSample.thermostat_id, func.max(RuntimeSample.at))
                                .group_by(RuntimeSample.thermostat_id)).all())

    # Group thermostats by first day to request, so most runs are one call per 25.
    groups: dict[date, list[int]] = {}
    for tid in therms:
        last = aware(newest.get(tid))
        since = last - REREAD if last else now - timedelta(days=st.history_backfill_days)
        groups.setdefault((since - timedelta(days=1)).date(), []).append(tid)

    backend, stored = get_backend(), 0
    ident_to_id = {ident: tid for tid, (ident, _) in therms.items()}
    for start, tids in sorted(groups.items()):
        idents = [therms[t][0] for t in tids]
        try:
            rows = backend.runtime_report(idents, start, now.date() + timedelta(days=1),
                                          {therms[t][0]: therms[t][1] for t in tids})
        except AuthRequired as err:
            record_auth_error(str(err))
            return {"error": str(err)}
        except BackendError as err:
            log.warning("ecobee runtime report failed: %s", err)
            return {"error": str(err), "stored": stored}
        stored += _store(rows, ident_to_id)

    with session_scope() as s:
        s.execute(delete(RuntimeSample).where(
            RuntimeSample.at < now - timedelta(days=st.history_keep_days)))
    return {"thermostats": len(therms), "stored": stored}


def _store(rows: list[RuntimeRow], ident_to_id: dict[str, int]) -> int:
    """Replace each thermostat's samples from its earliest returned interval onward."""
    by_therm: dict[int, list[RuntimeRow]] = {}
    for r in rows:
        if r.identifier in ident_to_id:
            by_therm.setdefault(ident_to_id[r.identifier], []).append(r)
    with session_scope() as s:
        for tid, rs in by_therm.items():
            first = min(r.at for r in rs)
            s.execute(delete(RuntimeSample).where(RuntimeSample.thermostat_id == tid,
                                                  RuntimeSample.at >= first))
            s.add_all(RuntimeSample(
                thermostat_id=tid, at=r.at, temp=r.temp, humidity=r.humidity, heat_sp=r.heat_sp,
                cool_sp=r.cool_sp, outdoor=r.outdoor, heat_s=r.heat_s, cool_s=r.cool_s,
                fan_s=r.fan_s, occupied=r.occupied) for r in {r.at: r for r in rs}.values())
    return sum(len(rs) for rs in by_therm.values())


def samples(thermostat_ids: list[int], since: datetime, until: datetime | None = None
            ) -> list[RuntimeSample]:
    with session_scope() as s:
        q = select(RuntimeSample).where(RuntimeSample.thermostat_id.in_(thermostat_ids),
                                        RuntimeSample.at >= since)
        if until:
            q = q.where(RuntimeSample.at < until)
        rows = list(s.scalars(q.order_by(RuntimeSample.at)))
        s.expunge_all()
        return rows
