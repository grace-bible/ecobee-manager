"""Alerts to a Google Chat webhook (docs/SPEC.md §9): offline, off target, login and
calendar failures, and ecobee's own device alerts (equipment faults, temperature alerts).

Every minute `check_once()` works out which incidents are true *right now*, opens an
`alert` row for each new one and resolves rows whose condition cleared. Chat gets one
message when an incident opens and one when it resolves — never a message per minute.

`find_incidents()` is pure (plain inputs, no I/O) so the thresholds are testable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.db import session_scope
from app.models import (
    Alert,
    CalendarSyncState,
    EcobeeCredentials,
    Thermostat,
    Zone,
    aware,
    utcnow,
)

log = logging.getLogger(__name__)
OCCUPIED_INTENTS = ("occupied", "custom")


@dataclass(frozen=True)
class ThermoIn:
    identifier: str
    name: str
    connected: bool
    last_seen: datetime | None
    current_temp: float | None = None
    # Resolved target now and how long it has been continuously "occupied" (None = not).
    heat: float | None = None
    cool: float | None = None
    occupied_for: timedelta | None = None
    ecobee_alerts: tuple[dict, ...] = ()  # active alerts on the device itself


@dataclass(frozen=True)
class SyncIn:
    calendar_id: str
    last_success: datetime | None
    last_error: str | None
    first_seen: datetime | None = None  # when the zone started needing this calendar


def find_incidents(
    now: datetime,
    thermostats: list[ThermoIn],
    ecobee_error: str | None,
    syncs: list[SyncIn],
    offline_after: timedelta = timedelta(minutes=15),
    setpoint_after: timedelta = timedelta(minutes=60),
    tolerance: float = 3.0,
    calendar_after: timedelta = timedelta(minutes=15),
) -> dict[str, tuple[str, str]]:
    """{key: (kind, message)} for everything currently wrong."""
    out: dict[str, tuple[str, str]] = {}
    if ecobee_error:
        out["ecobee_auth"] = ("ecobee_auth", f"ecobee login is failing: {ecobee_error}")

    for t in thermostats:
        if not t.connected and t.last_seen and now - t.last_seen >= offline_after:
            mins = int((now - t.last_seen).total_seconds() // 60)
            out[f"offline:{t.identifier}"] = (
                "offline", f"{t.name} has been offline for {mins} min")
        if (t.connected and t.occupied_for is not None and t.occupied_for >= setpoint_after
                and t.current_temp is not None and t.heat is not None and t.cool is not None):
            mins = int(t.occupied_for.total_seconds() // 60)
            if t.current_temp < t.heat - tolerance:
                msg = f"{t.name} is {t.current_temp:g}°F, heating to {t.heat:g}°F for {mins} min"
                out[f"setpoint:{t.identifier}"] = ("setpoint", msg)
            elif t.current_temp > t.cool + tolerance:
                msg = f"{t.name} is {t.current_temp:g}°F, cooling to {t.cool:g}°F for {mins} min"
                out[f"setpoint:{t.identifier}"] = ("setpoint", msg)

        for a in t.ecobee_alerts:
            ref = a.get("ref") or a.get("text", "")[:60]
            out[f"ecobee:{t.identifier}:{ref}"] = ("ecobee", f"{t.name}: {a.get('text', '')}")

    for s in syncs:
        if not s.last_error:
            continue
        since = s.last_success or s.first_seen
        if since is None or now - since >= calendar_after:
            out[f"calendar:{s.calendar_id}"] = (
                "calendar", f"Calendar sync failing for {s.calendar_id}: {s.last_error[:300]}")
    return out


# ---------- gathering (I/O) ----------

def _occupied_for(cfg, now, ovs, events, blocks, deadband, look_back: timedelta,
                  step=timedelta(minutes=5)) -> timedelta | None:
    """How long the zone has resolved to an occupied intent without a break (≤ look_back)."""
    from app.resolver import resolve
    if resolve(cfg, now, ovs, events, blocks, deadband).intent not in OCCUPIED_INTENTS:
        return None
    t = now
    while now - t < look_back:
        prev = t - step
        if resolve(cfg, prev, ovs, events, blocks, deadband).intent not in OCCUPIED_INTENTS:
            break
        t = prev
    return now - t


def gather(now: datetime) -> dict[str, tuple[str, str]]:
    from app.reconciler import gather_inputs, zone_cfg
    from app.resolver import resolve

    st = get_settings()
    setpoint_after = timedelta(minutes=st.alert_setpoint_minutes)
    therms: list[ThermoIn] = []
    with session_scope() as s:
        managed: set[int] = set()
        zones = s.scalars(select(Zone).where(Zone.enabled.is_(True)).options(
            selectinload(Zone.thermostats), selectinload(Zone.groups),
            selectinload(Zone.campus), selectinload(Zone.rooms))).all()
        for z in zones:
            cfg = zone_cfg(z)
            # Look back far enough that blocks/events that began an hour ago are included.
            zone_ovs, therm_ovs, events, blocks = gather_inputs(
                s, z, now - setpoint_after, setpoint_after + timedelta(minutes=5))
            for t in z.thermostats:
                managed.add(t.id)
                ovs = zone_ovs + therm_ovs.get(t.id, [])
                d = resolve(cfg, now, ovs, events, blocks, st.min_deadband_f)
                occ = _occupied_for(cfg, now, ovs, events, blocks, st.min_deadband_f,
                                    setpoint_after)
                therms.append(ThermoIn(t.identifier, t.name, t.connected, aware(t.last_seen),
                                       t.current_temp, d.heat, d.cool, occ,
                                       tuple(t.ecobee_alerts or ())))
        for t in s.scalars(select(Thermostat)):
            if t.id not in managed:
                therms.append(ThermoIn(t.identifier, t.name, t.connected, aware(t.last_seen),
                                       ecobee_alerts=tuple(t.ecobee_alerts or ())))

        creds = s.get(EcobeeCredentials, 1)
        ecobee_error = creds.last_error if creds else None
        wanted = {c for z in zones for c in z.calendar_ids}
        syncs = [SyncIn(r.calendar_id, aware(r.last_success), r.last_error)
                 for r in s.scalars(select(CalendarSyncState)) if r.calendar_id in wanted]

    return find_incidents(
        now, therms, ecobee_error, syncs,
        offline_after=timedelta(minutes=st.alert_offline_minutes),
        setpoint_after=setpoint_after, tolerance=st.alert_setpoint_tolerance_f,
        calendar_after=timedelta(minutes=st.alert_calendar_minutes))


def send_chat(text: str) -> bool:
    url = get_settings().google_chat_webhook
    if not url:
        return False
    try:
        httpx.post(url, json={"text": text}, timeout=10).raise_for_status()
        return True
    except httpx.HTTPError as err:
        log.warning("Google Chat webhook failed: %s", err)
        return False


def check_once(now: datetime | None = None) -> dict:
    now = now or utcnow()
    current = gather(now)
    opened, resolved = [], []
    with session_scope() as s:
        open_rows = {a.key: a for a in s.scalars(select(Alert).where(Alert.resolved_at.is_(None)))}
        for key, (kind, msg) in current.items():
            if key not in open_rows:
                a = Alert(key=key, kind=kind, message=msg, opened_at=now)
                s.add(a)
                opened.append(a)
        for key, a in open_rows.items():
            if key not in current:
                a.resolved_at = now
                resolved.append(a)
        s.flush()
        # Retry a failed open notification next minute; a resolve is best-effort.
        for a in [*opened, *(a for a in open_rows.values() if not a.notified and not a.resolved_at)]:
            a.notified = send_chat(f"⚠️ {a.message}")
        for a in resolved:
            if a.notified:
                send_chat(f"✅ Resolved: {a.message}")
    return {"open": len(current), "opened": len(opened), "resolved": len(resolved)}


def open_alerts() -> list[Alert]:
    with session_scope() as s:
        return list(s.scalars(select(Alert).where(Alert.resolved_at.is_(None))
                              .order_by(Alert.opened_at)))
