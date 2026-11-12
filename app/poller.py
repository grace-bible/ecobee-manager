"""ecobee → database. Cheap summary every minute; full fetch only when a thermostat's
revision changed, and never more than once per 3 minutes per thermostat."""

import logging
from datetime import timedelta

from sqlalchemy import select

from app.db import session_scope
from app.ecobee.backend import AuthRequired, BackendError
from app.models import Thermostat, aware, utcnow
from app.services import get_backend, record_auth_error

log = logging.getLogger(__name__)
MIN_FETCH_INTERVAL = timedelta(minutes=3)


def poll_once() -> dict:
    backend = get_backend()
    try:
        rows = backend.summary()
    except AuthRequired as err:
        record_auth_error(str(err))
        log.error("ecobee auth needs attention: %s", err)
        return {"error": str(err)}
    except BackendError as err:
        log.warning("ecobee summary failed: %s", err)
        return {"error": str(err)}

    now = utcnow()
    to_fetch: list[str] = []
    revisions: dict[str, str] = {}
    with session_scope() as s:
        known = {t.identifier: t for t in s.scalars(select(Thermostat))}
        for r in rows:
            t = known.get(r.identifier)
            if t is None:
                t = Thermostat(identifier=r.identifier, name=r.name)
                s.add(t)
            t.name, t.connected, t.equipment_running = r.name, r.connected, r.equipment
            if r.connected:
                t.last_seen = now
            stale = t.last_full_fetch is None or now - aware(t.last_full_fetch) >= MIN_FETCH_INTERVAL
            if r.revision != t.revision and stale:
                to_fetch.append(r.identifier)
                # Recorded only after a successful fetch, so a failed one is retried.
                revisions[r.identifier] = r.revision

    if to_fetch:
        try:
            states = backend.fetch(to_fetch)
        except BackendError as err:
            log.warning("ecobee fetch failed: %s", err)
            return {"summary": len(rows), "error": str(err)}
        with session_scope() as s:
            by_id = {t.identifier: t for t in s.scalars(
                select(Thermostat).where(Thermostat.identifier.in_(to_fetch)))}
            for st in states:
                t = by_id[st.identifier]
                t.model, t.connected = st.model, st.connected
                t.current_temp, t.humidity = st.current_temp, st.humidity
                t.heat_setpoint, t.cool_setpoint = st.heat_setpoint, st.cool_setpoint
                t.hvac_mode, t.equipment_running = st.hvac_mode, st.equipment_running
                t.active_hold, t.last_full_fetch = st.active_hold, now
                t.revision = revisions[st.identifier]
    return {"summary": len(rows), "fetched": len(to_fetch)}
