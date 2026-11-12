"""Google Workspace resource calendars → `calendar_event` rows.

Auth: a service account with domain-wide delegation, impersonating GOOGLE_IMPERSONATE.
Scopes to grant in Admin console → Security → API controls → Domain-wide delegation:
  https://www.googleapis.com/auth/calendar.readonly
  https://www.googleapis.com/auth/admin.directory.resource.calendar.readonly
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache

from sqlalchemy import delete, select

from app.config import get_settings
from app.db import session_scope
from app.models import CalendarEvent, CalendarSyncState, Zone, utcnow
from app.resolver import parse_hvac_tags

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/admin.directory.resource.calendar.readonly",
]


@dataclass
class Resource:
    email: str  # resourceEmail — this is the calendar id
    name: str
    building: str | None
    capacity: int | None


def configured() -> bool:
    st = get_settings()
    return bool((st.google_service_account_json or st.google_service_account_file)
                and st.google_impersonate)


@lru_cache
def _creds():
    from google.oauth2 import service_account

    st = get_settings()
    if st.google_service_account_json:
        import json
        return service_account.Credentials.from_service_account_info(
            json.loads(st.google_service_account_json), scopes=SCOPES,
            subject=st.google_impersonate)
    return service_account.Credentials.from_service_account_file(
        st.google_service_account_file, scopes=SCOPES, subject=st.google_impersonate)


def _service(name: str, version: str):
    from googleapiclient.discovery import build

    return build(name, version, credentials=_creds(), cache_discovery=False)


def list_resources() -> list[Resource]:
    """All Workspace room resources, for the zone → calendar picker."""
    svc = _service("admin", "directory_v1")
    out, token = [], None
    while True:
        resp = svc.resources().calendars().list(
            customer=get_settings().google_customer_id, maxResults=500, pageToken=token).execute()
        for r in resp.get("items", []):
            out.append(Resource(r["resourceEmail"], r.get("resourceName", r["resourceEmail"]),
                                r.get("buildingId"), r.get("capacity")))
        token = resp.get("nextPageToken")
        if not token:
            return sorted(out, key=lambda r: ((r.building or ""), r.name))


@dataclass
class WorkspaceBuilding:
    id: str  # buildingId, as on each room resource
    name: str


def list_buildings() -> list[WorkspaceBuilding]:
    """Workspace buildings (names for the buildingIds on room resources)."""
    svc = _service("admin", "directory_v1")
    out, token = [], None
    while True:
        resp = svc.resources().buildings().list(
            customer=get_settings().google_customer_id, maxResults=500, pageToken=token).execute()
        out += [WorkspaceBuilding(b["buildingId"], b.get("buildingName") or b["buildingId"])
                for b in resp.get("buildings", [])]
        token = resp.get("nextPageToken")
        if not token:
            return out


def _parse_time(t: dict) -> datetime:
    if "dateTime" in t:
        return datetime.fromisoformat(t["dateTime"])
    # All-day event: treat as the whole local day. Rooms rarely book all-day, but a
    # retreat or VBS might.
    from zoneinfo import ZoneInfo
    return datetime.fromisoformat(t["date"]).replace(
        tzinfo=ZoneInfo(t.get("timeZone") or get_settings().default_timezone))


def _upsert(s, cal_id: str, ev: dict) -> None:
    existing = s.scalar(select(CalendarEvent).where(
        CalendarEvent.calendar_id == cal_id, CalendarEvent.event_id == ev["id"]))
    if ev.get("status") == "cancelled":
        if existing:
            s.delete(existing)
        return
    # The room itself may have declined (conflict / not allowed). Then it isn't happening there.
    room = next((a for a in ev.get("attendees", []) if a.get("email") == cal_id), None)
    if room and room.get("responseStatus") == "declined":
        if existing:
            s.delete(existing)
        return
    row = existing or CalendarEvent(calendar_id=cal_id, event_id=ev["id"])
    row.title = (ev.get("summary") or "")[:500]
    row.starts_at = _parse_time(ev["start"]).astimezone(UTC)
    row.ends_at = _parse_time(ev["end"]).astimezone(UTC)
    row.status = ev.get("status", "confirmed")
    row.hvac_tag = parse_hvac_tags(ev.get("summary"), ev.get("description"))
    s.add(row)


def sync_calendar(cal_id: str) -> int:
    """Incremental sync with syncToken; full window re-sync on first run or 410."""
    svc = _service("calendar", "v3")
    st = get_settings()
    with session_scope() as s:
        state = s.get(CalendarSyncState, cal_id) or CalendarSyncState(calendar_id=cal_id)
        s.add(state)
        token = state.sync_token

    def full_params():
        now = utcnow()
        return {"timeMin": (now - timedelta(days=1)).isoformat(),
                "timeMax": (now + timedelta(days=st.calendar_window_days)).isoformat()}

    params = {"syncToken": token} if token else full_params()
    changed, page = 0, None
    try:
        while True:
            try:
                resp = svc.events().list(calendarId=cal_id, singleEvents=True, showDeleted=True,
                                         maxResults=250, pageToken=page, **params).execute()
            except Exception as err:  # googleapiclient.errors.HttpError
                if getattr(getattr(err, "resp", None), "status", None) == 410 and "syncToken" in params:
                    log.info("sync token expired for %s; full re-sync", cal_id)
                    with session_scope() as s:
                        s.execute(delete(CalendarEvent).where(CalendarEvent.calendar_id == cal_id))
                    params, page = full_params(), None
                    continue
                raise
            with session_scope() as s:
                for ev in resp.get("items", []):
                    _upsert(s, cal_id, ev)
                    changed += 1
            page = resp.get("nextPageToken")
            if not page:
                with session_scope() as s:
                    state = s.get(CalendarSyncState, cal_id)
                    state.sync_token = resp.get("nextSyncToken")
                    state.last_success, state.last_error = utcnow(), None
                return changed
    except Exception as err:
        with session_scope() as s:
            state = s.get(CalendarSyncState, cal_id)
            state.last_error = str(err)[:2000]
        raise


def wanted_calendars(s) -> list[str]:
    """Calendars of rooms that an enabled zone serves."""
    return sorted({c for z in s.scalars(select(Zone).where(Zone.enabled.is_(True)))
                   for c in z.calendar_ids})


def sync_all() -> dict:
    """Sync every room calendar served by an enabled zone. One failure doesn't stop the rest.

    Note: a syncToken keeps returning changes forever, so the 14-day window only bounds the
    first sync. Old rows are pruned here so the table doesn't grow without limit."""
    if not configured():
        return {"skipped": "google not configured"}
    with session_scope() as s:
        cals = wanted_calendars(s)
        s.execute(delete(CalendarEvent).where(CalendarEvent.ends_at < utcnow() - timedelta(days=2)))
    result = {}
    for cal in cals:
        try:
            result[cal] = sync_calendar(cal)
        except Exception as err:
            log.warning("calendar sync failed for %s: %s", cal, err)
            result[cal] = f"error: {err}"
    return result
