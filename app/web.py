"""Server-rendered UI (Jinja + HTMX). Mutations reuse the API's validation and helpers."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, available_timezones

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app import alerts, api, gcal, reconciler
from app.auth import Principal, current_principal, require_admin, require_write
from app.charts import room_chart
from app.config import get_settings
from app.db import get_db
from app.ecobee.backend import AuthRequired, BackendError
from app.insights import downsample
from app.models import (
    Alert,
    ApiKey,
    AppUser,
    AuditLog,
    Building,
    CalendarEvent,
    CalendarSyncState,
    Campus,
    EcobeeCredentials,
    Override,
    Room,
    ScheduleBlock,
    Thermostat,
    Zone,
    ZoneGroup,
    aware,
    utcnow,
)
from app.services import get_backend, record_auth_error

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _fmt_local(dt: datetime | None, tz: str | None = None) -> str:
    if not dt:
        return "—"
    local = aware(dt).astimezone(ZoneInfo(tz or get_settings().default_timezone))
    return local.strftime("%a %-I:%M %p")


def _fmt_when(dt: datetime | None, tz: str | None = None) -> str:
    """'3:15 PM' today, 'tomorrow 8 AM', else 'Fri 8 AM'. For people, not logs."""
    if not dt:
        return "—"
    zone = ZoneInfo(tz or get_settings().default_timezone)
    local, today = aware(dt).astimezone(zone), datetime.now(zone).date()
    clock = local.strftime("%-I:%M %p").replace(":00 ", " ")
    if local.date() == today:
        return clock
    if local.date() == today + timedelta(days=1):
        return f"tomorrow {clock}"
    return f"{local:%a} {clock}"


def _fmt_deg(v: float | None) -> str:
    return "—" if v is None else f"{v:.1f}".removesuffix(".0")


INTENTS = {"occupied": "In use", "unoccupied": "Empty", "off": "Off", "custom": "Custom"}
SOURCES = {"override": "Override", "calendar": "Booking", "schedule": "Weekly schedule",
           "default": "No schedule"}

templates.env.filters["local"] = _fmt_local
templates.env.filters["when"] = _fmt_when
templates.env.filters["deg"] = _fmt_deg
templates.env.filters["zip_sum"] = lambda a, b: [x + y for x, y in zip(a, b, strict=True)]
templates.env.globals["INTENTS"] = INTENTS
templates.env.globals["SOURCES"] = SOURCES
# Cache-bust the stylesheet: browsers keep /static/app.css otherwise.
templates.env.globals["css_version"] = int(
    (Path(__file__).parent / "static" / "app.css").stat().st_mtime)
templates.env.globals["settings"] = get_settings
templates.env.globals["open_alerts"] = alerts.open_alerts


def render(request: Request, name: str, p: Principal, **ctx):
    return templates.TemplateResponse(request, name, {"me": p, **ctx})


def _back(request: Request, fallback: str = "/") -> RedirectResponse:
    return RedirectResponse(request.headers.get("referer") or fallback, status_code=303)


def _campus_tree(db: Session, campus: int | None = None):
    q = select(Campus).order_by(Campus.name).options(
        selectinload(Campus.zones).selectinload(Zone.thermostats),
        selectinload(Campus.zones).selectinload(Zone.rooms),
        selectinload(Campus.buildings), selectinload(Campus.rooms))
    campuses = db.scalars(q).all()
    return campuses, [c for c in campuses if campus is None or c.id == campus]


@router.get("/", response_class=HTMLResponse)
def rooms(request: Request, campus: int | None = None, db: Session = Depends(get_db),
          p: Principal = Depends(current_principal)):
    """The everyday screen: campus → building → zone cards, with one-tap changes."""
    campuses, shown = _campus_tree(db, campus)
    now = utcnow()
    status = {z.id: api.zone_status(db, z, now) for c in shown for z in c.zones if z.enabled}
    groups = db.scalars(select(ZoneGroup).order_by(ZoneGroup.name)
                        .options(selectinload(ZoneGroup.zones))).all()
    groups = [g for g in groups if g.zones and (campus is None or any(
        z.campus_id == campus for z in g.zones))]
    unassigned = db.scalar(select(func.count()).select_from(Thermostat)
                           .where(Thermostat.zone_id.is_(None)))
    return render(request, "rooms.html", p, campuses=campuses, shown=shown, status=status,
                  groups=groups, campus=campus, unassigned=unassigned)


@router.get("/buildings/{building_id}", response_class=HTMLResponse)
def building_page(request: Request, building_id: int, db: Session = Depends(get_db),
                  p: Principal = Depends(current_principal)):
    b = db.get(Building, building_id)
    if not b:
        raise HTTPException(404)
    now = utcnow()
    blocks = db.scalars(select(ScheduleBlock).where(ScheduleBlock.building_id == b.id)
                        .order_by(ScheduleBlock.start)).all()
    return render(request, "building.html", p, building=b,
                  status={z.id: api.zone_status(db, z, now) for z in b.zones if z.enabled},
                  blocks=[api.block_dict(x) for x in blocks],
                  editable=p.can_write and p.may_touch_campus(b.campus_id))


@router.get("/insights", response_class=HTMLResponse)
def insights_page(request: Request, days: int = 7, db: Session = Depends(get_db),
                  p: Principal = Depends(current_principal)):
    """Every room over the last week: heating/cooling hours, warm-up times, empty rooms."""
    days = 30 if days >= 30 else 7
    campuses = db.scalars(select(Campus).order_by(Campus.name).options(
        selectinload(Campus.zones).selectinload(Zone.thermostats))).all()
    now = utcnow()
    rows = {z.id: api.room_insights(z, now, days) for c in campuses for z in c.zones
            if z.enabled}
    alerts_by_room = {z.id: [(t.name, a) for t in z.thermostats for a in (t.ecobee_alerts or [])]
                      for c in campuses for z in c.zones}
    return render(request, "insights.html", p, campuses=campuses, rows=rows, days=days,
                  alerts_by_room=alerts_by_room)


@router.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request, db: Session = Depends(get_db),
               p: Principal = Depends(current_principal)):
    return render_setup(request, "setup.html", p, db, current=("overview", None),
                        google=gcal.configured())


def render_setup(request: Request, name: str, p: Principal, db: Session,
                 current: tuple[str, int | None] | None = None, **ctx):
    """Setup pages share a tree (campus → building → zone → rooms) beside the detail."""
    campuses, _ = _campus_tree(db)
    unassigned = db.scalars(select(Thermostat).where(Thermostat.zone_id.is_(None))
                            .order_by(Thermostat.name)).all()
    return render(request, name, p, tree=campuses, campuses=campuses, unassigned=unassigned,
                  current=current or ("", None), msg=request.query_params.get("msg"), **ctx)


@router.get("/setup/campuses/{campus_id}", response_class=HTMLResponse)
def campus_setup_page(request: Request, campus_id: int, db: Session = Depends(get_db),
                      p: Principal = Depends(current_principal)):
    c = db.get(Campus, campus_id)
    if not c:
        raise HTTPException(404)
    return render_setup(request, "campus_edit.html", p, db, current=("campus", c.id), campus=c,
                        timezones=sorted(available_timezones()))


@router.get("/setup/buildings/{building_id}", response_class=HTMLResponse)
def building_setup_page(request: Request, building_id: int, db: Session = Depends(get_db),
                        p: Principal = Depends(current_principal)):
    b = db.get(Building, building_id)
    if not b:
        raise HTTPException(404)
    return render_setup(request, "building_edit.html", p, db, current=("building", b.id),
                        building=b, editable=p.can_write and p.may_touch_campus(b.campus_id))


@router.get("/zones/new", response_class=HTMLResponse)
def new_zone_page(request: Request, db: Session = Depends(get_db),
                  p: Principal = Depends(current_principal)):
    return zone_edit_page(request, None, db, p)


RANGES = {"24h": (24, 5), "3d": (72, 5), "7d": (168, 15), "30d": (720, 60)}  # hours, bucket min


@router.get("/zones/{zone_id}", response_class=HTMLResponse)
def room_page(request: Request, zone_id: int, db: Session = Depends(get_db),
              p: Principal = Depends(current_principal)):
    zone = db.get(Zone, zone_id)
    if not zone:
        raise HTTPException(404)
    now = utcnow()
    room_of = {r.calendar_id: r.name for r in zone.rooms if r.calendar_id}
    events = db.scalars(select(CalendarEvent).where(
        CalendarEvent.calendar_id.in_(list(room_of)), CalendarEvent.ends_at > now)
        .order_by(CalendarEvent.starts_at).limit(12)).all() if room_of else []
    blocks = db.scalars(select(ScheduleBlock).where(ScheduleBlock.zone_id == zone.id)
                        .order_by(ScheduleBlock.start)).all()
    inherited = db.scalars(select(ScheduleBlock).where(
        (ScheduleBlock.group_id.in_([g.id for g in zone.groups]))
        | (ScheduleBlock.building_id == zone.building_id if zone.building_id else False))
        .order_by(ScheduleBlock.start)).all()
    rng = request.query_params.get("range", "24h")
    hours, bucket = RANGES.get(rng, RANGES["24h"])
    tz = zone.campus.timezone if zone.campus else get_settings().default_timezone
    start = now - timedelta(hours=hours)
    pts = downsample(api.room_points(zone, start), timedelta(minutes=bucket))
    chart = room_chart(pts, start, now, tz, gap=timedelta(minutes=bucket * 2 + 6))
    syncs = [db.get(CalendarSyncState, c) for c in room_of]
    return render(request, "room.html", p, zone=zone, s=api.zone_status(db, zone, now),
                  chart=chart, range=rng if rng in RANGES else "24h", ranges=list(RANGES),
                  insights=api.room_insights(zone, now, days=max(hours // 24, 1)),
                  range_label={"24h": "last 24 hours", "3d": "last 3 days", "7d": "last 7 days",
                               "30d": "last 30 days"}.get(rng, "last 24 hours"),
                  preview=_preview(db, zone, now, hours=36), events=events, room_of=room_of,
                  blocks=[api.block_dict(b) for b in blocks],
                  inherited=[api.block_dict(b) for b in inherited],
                  thermostats=sorted(zone.thermostats, key=lambda t: t.name),
                  last_sync=min((x.last_success for x in syncs if x and x.last_success),
                                default=None),
                  editable=p.can_write and p.may_touch_campus(zone.campus_id))


@router.get("/zones/{zone_id}/edit", response_class=HTMLResponse)
def zone_edit_page(request: Request, zone_id: int | None, db: Session = Depends(get_db),
                   p: Principal = Depends(current_principal)):
    zone = db.get(Zone, zone_id) if zone_id else None
    if zone_id and not zone:
        raise HTTPException(404)
    thermostats = db.scalars(select(Thermostat).where(
        (Thermostat.zone_id.is_(None)) | (Thermostat.zone_id == zone_id))
        .order_by(Thermostat.name)).all()
    editable = p.can_write and (zone is None or p.may_touch_campus(zone.campus_id))
    return render_setup(request, "zone_edit.html", p, db,
                        current=("zone", zone.id) if zone else ("new-zone", None), zone=zone,
                        thermostats=thermostats, editable=editable,
                  preset_campus=_int(request.query_params.get("campus")),
                  preset_building=_int(request.query_params.get("building")))


def _int(v) -> int | None:
    try:
        return int(v) if v not in (None, "") else None
    except ValueError:
        return None


def _preview(db: Session, zone: Zone, now: datetime, hours: int = 48) -> list[dict]:
    """Resolved target over the next `hours`, collapsed into spans, for the zone page."""
    from app.resolver import resolve
    cfg = reconciler.zone_cfg(zone)
    horizon = timedelta(hours=hours)
    zone_ovs, _, events, blocks = reconciler.gather_inputs(db, zone, now, horizon)
    # Step on the quarter hour so an 08:00 block shows as 08:00, not 08:14.
    start = now.replace(minute=now.minute - now.minute % 15, second=0, microsecond=0)
    spans, t, step = [], start, timedelta(minutes=15)
    while t < now + horizon:
        d = resolve(cfg, t, zone_ovs, events, blocks, get_settings().min_deadband_f)
        if spans and spans[-1]["key"] == (d.key, d.source):
            spans[-1]["end"] = t + step
        else:
            spans.append({"key": (d.key, d.source), "start": t, "end": t + step,
                          "heat": d.heat, "cool": d.cool, "source": d.source, "intent": d.intent})
        t += step
    return spans


@router.post("/zones")
@router.post("/zones/{zone_id}")
async def save_zone(request: Request, zone_id: int | None = None, db: Session = Depends(get_db),
                    p: Principal = Depends(require_write)):
    form = await request.form()
    data = {k: v for k, v in form.items()
            if k not in ("thermostat_ids", "room_ids") and v != ""}
    data["enabled"] = "enabled" in form
    data["thermostat_ids"] = [int(x) for x in form.getlist("thermostat_ids")]
    data["room_ids"] = [int(x) for x in form.getlist("room_ids")]
    try:
        body = api.ZoneIn(**data)
    except ValidationError as err:
        raise HTTPException(422, err.errors()[0]["msg"]) from err
    if zone_id:
        api.update_zone(zone_id, body, db, p)
    else:
        zone_id = api.create_zone(body, db, p)["id"]
    return RedirectResponse(f"/zones/{zone_id}/edit?msg=Saved", status_code=303)


@router.post("/campuses")
def add_campus(name: str = Form(...), timezone: str = Form("America/Chicago"),
               db: Session = Depends(get_db), p: Principal = Depends(require_admin)):
    try:
        body = api.CampusIn(name=name.strip(), timezone=timezone.strip())
    except ValidationError as err:
        raise HTTPException(422, err.errors()[0]["msg"].removeprefix("Value error, ")) from err
    cid = api.create_campus(body, db, p)["id"]
    return RedirectResponse(f"/setup/campuses/{cid}?msg=Campus added", status_code=303)


@router.post("/campuses/{campus_id}")
def save_campus(campus_id: int, name: str = Form(...), timezone: str = Form(...),
                db: Session = Depends(get_db), p: Principal = Depends(require_admin)):
    try:
        body = api.CampusIn(name=name.strip(), timezone=timezone.strip())
    except ValidationError as err:
        raise HTTPException(422, err.errors()[0]["msg"].removeprefix("Value error, ")) from err
    api.update_campus(campus_id, body, db, p)
    return RedirectResponse(f"/setup/campuses/{campus_id}?msg=Saved", status_code=303)


@router.post("/campuses/{campus_id}/delete")
def remove_campus(campus_id: int, db: Session = Depends(get_db),
                  p: Principal = Depends(require_admin)):
    api.delete_campus(campus_id, db, p)
    return RedirectResponse("/setup?msg=Campus removed", status_code=303)


@router.post("/thermostats/{thermostat_id}/zone")
def assign_thermostat(request: Request, thermostat_id: int, zone_id: str = Form(""),
                      db: Session = Depends(get_db), p: Principal = Depends(require_write)):
    t = api.set_thermostat_zone(thermostat_id, api.ThermostatZoneIn(zone_id=_int(zone_id)), db, p)
    return _back(request, f"/zones/{t['zone_id']}/edit" if t["zone_id"] else "/setup")


@router.post("/zones/{zone_id}/delete")
def delete_zone(zone_id: int, db: Session = Depends(get_db),
                p: Principal = Depends(require_write)):
    z = db.get(Zone, zone_id)
    back = f"/setup/buildings/{z.building_id}" if z and z.building_id else (
        f"/setup/campuses/{z.campus_id}" if z else "/setup")
    api.delete_zone(zone_id, db, p)
    return RedirectResponse(f"{back}?msg=Zone removed", status_code=303)


# ---------- buildings & rooms (setup) ----------

@router.post("/buildings")
@router.post("/buildings/{building_id}")
def save_building(building_id: int | None = None, name: str = Form(...),
                  campus_id: int = Form(...), db: Session = Depends(get_db),
                  p: Principal = Depends(require_write)):
    body = api.BuildingIn(name=name.strip(), campus_id=campus_id)
    if building_id:
        api.update_building(building_id, body, db, p)
    else:
        building_id = api.create_building(body, db, p)["id"]
    return RedirectResponse(f"/setup/buildings/{building_id}?msg=Saved", status_code=303)


@router.post("/buildings/{building_id}/delete")
def remove_building(building_id: int, db: Session = Depends(get_db),
                    p: Principal = Depends(require_write)):
    b = db.get(Building, building_id)
    cid = b.campus_id if b else None
    api.delete_building(building_id, db, p)
    return RedirectResponse(f"/setup/campuses/{cid}?msg=Building removed" if cid else "/setup",
                            status_code=303)


@router.get("/rooms/new", response_class=HTMLResponse)
@router.get("/rooms/{room_id}/edit", response_class=HTMLResponse)
def room_edit_page(request: Request, room_id: int | None = None, db: Session = Depends(get_db),
                   p: Principal = Depends(current_principal)):
    room = db.get(Room, room_id) if room_id else None
    if room_id and not room:
        raise HTTPException(404)
    resources, resource_error = [], None
    if gcal.configured():
        try:
            resources = gcal.list_resources()
        except Exception as err:  # show the page anyway; the id can be typed in
            resource_error = str(err)
    return render_setup(request, "room_edit.html", p, db,
                        current=("room", room.id) if room else ("new-room", None), room=room,
                        resources=resources, resource_error=resource_error,
                  preset_campus=_int(request.query_params.get("campus")),
                  preset_building=_int(request.query_params.get("building")),
                  editable=p.can_write and (room is None or p.may_touch_campus(room.campus_id)))


@router.post("/rooms")
@router.post("/rooms/{room_id}")
async def save_room(request: Request, room_id: int | None = None,
                    db: Session = Depends(get_db), p: Principal = Depends(require_write)):
    form = await request.form()
    try:
        body = api.RoomIn(name=(form.get("name") or "").strip(),
                          campus_id=int(form["campus_id"]),
                          building_id=_int(form.get("building_id")),
                          calendar_id=(form.get("calendar_id") or "").strip() or None,
                          capacity=_int(form.get("capacity")),
                          zone_ids=[int(x) for x in form.getlist("zone_ids")])
    except (KeyError, ValueError, ValidationError) as err:
        raise HTTPException(422, "a room needs a name and a campus") from err
    if room_id:
        api.update_room(room_id, body, db, p)
    else:
        room_id = api.create_room(body, db, p)["id"]
    return RedirectResponse(f"/rooms/{room_id}/edit?msg=Saved", status_code=303)


@router.post("/rooms/{room_id}/delete")
def remove_room(room_id: int, db: Session = Depends(get_db),
                p: Principal = Depends(require_write)):
    r = db.get(Room, room_id)
    back = f"/setup/buildings/{r.building_id}" if r and r.building_id else (
        f"/setup/campuses/{r.campus_id}" if r else "/setup")
    api.delete_room(room_id, db, p)
    return RedirectResponse(f"{back}?msg=Room removed", status_code=303)


@router.get("/setup/import", response_class=HTMLResponse)
def import_page(request: Request, db: Session = Depends(get_db),
                p: Principal = Depends(require_admin)):
    try:
        gbuildings, error = (api.google_buildings(p) if gcal.configured() else []), None
    except Exception as err:
        gbuildings, error = [], str(err)
    return render_setup(request, "import.html", p, db, current=("import", None),
                        gbuildings=gbuildings, error=error, google=gcal.configured())


@router.post("/setup/import")
async def run_import(request: Request, db: Session = Depends(get_db),
                     p: Principal = Depends(require_admin)):
    form = await request.form()
    mapping = {k.removeprefix("b:"): int(v) for k, v in form.items()
               if k.startswith("b:") and v}
    r = api.import_google_rooms(db, p, api.GoogleImportIn(campuses=mapping))
    return RedirectResponse(f"/setup?msg=Imported {r['rooms']} rooms and {r['buildings']} "
                            "buildings from Google", status_code=303)


def _campuses_with_zones(db: Session):
    return db.scalars(select(Campus).order_by(Campus.name)
                      .options(selectinload(Campus.zones))).all()


@router.get("/groups", response_class=HTMLResponse)
def groups_page(request: Request, db: Session = Depends(get_db),
                p: Principal = Depends(current_principal)):
    groups = db.scalars(select(ZoneGroup).order_by(ZoneGroup.name)
                        .options(selectinload(ZoneGroup.zones))).all()
    return render(request, "groups.html", p, groups=groups, campuses=_campuses_with_zones(db))


@router.get("/groups/{group_id}", response_class=HTMLResponse)
def group_page(request: Request, group_id: int, db: Session = Depends(get_db),
               p: Principal = Depends(current_principal)):
    """Operator view: the group's zones, one-tap changes and its weekly schedule."""
    g = db.get(ZoneGroup, group_id)
    if not g:
        raise HTTPException(404)
    blocks = db.scalars(select(ScheduleBlock).where(ScheduleBlock.group_id == g.id)
                        .order_by(ScheduleBlock.start)).all()
    now = utcnow()
    editable = p.can_write and all(p.may_touch_campus(z.campus_id) for z in g.zones)
    return render(request, "group.html", p, group=g,
                  status={z.id: api.zone_status(db, z, now) for z in g.zones if z.enabled},
                  blocks=[api.block_dict(b) for b in blocks], editable=editable)


@router.get("/groups/{group_id}/edit", response_class=HTMLResponse)
def group_edit_page(request: Request, group_id: int, db: Session = Depends(get_db),
                    p: Principal = Depends(current_principal)):
    g = db.get(ZoneGroup, group_id)
    if not g:
        raise HTTPException(404)
    editable = p.can_write and all(p.may_touch_campus(z.campus_id) for z in g.zones)
    return render(request, "group_edit.html", p, group=g, campuses=_campuses_with_zones(db),
                  editable=editable)


def _group_body(form) -> api.GroupIn:
    try:
        return api.GroupIn(name=form["name"], description=form.get("description") or None,
                           zone_ids=sorted({int(x) for x in form.getlist("zone_ids")}))
    except (KeyError, ValidationError) as err:
        raise HTTPException(422, "a group needs a name") from err


@router.post("/groups")
async def add_group(request: Request, db: Session = Depends(get_db),
                    p: Principal = Depends(require_write)):
    gid = api.create_group(_group_body(await request.form()), db, p)["id"]
    return RedirectResponse(f"/groups/{gid}/edit", status_code=303)


@router.post("/groups/{group_id}")
async def save_group(request: Request, group_id: int, db: Session = Depends(get_db),
                     p: Principal = Depends(require_write)):
    api.update_group(group_id, _group_body(await request.form()), db, p)
    return RedirectResponse(f"/groups/{group_id}/edit?saved=1", status_code=303)


@router.post("/groups/{group_id}/delete")
def delete_group(group_id: int, db: Session = Depends(get_db),
                 p: Principal = Depends(require_write)):
    api.delete_group(group_id, db, p)
    return RedirectResponse("/groups", status_code=303)


@router.post("/schedule-blocks")
async def add_block(request: Request, db: Session = Depends(get_db),
                    p: Principal = Depends(require_write)):
    form = await request.form()
    weekdays = sum({int(x) for x in form.getlist("weekdays")})
    data = {k: form.get(k) or None for k in ("zone_id", "group_id", "building_id", "start",
                                             "end", "mode", "heat", "cool", "label")}
    try:
        body = api.BlockIn(weekdays=weekdays, **data)
    except ValidationError as err:
        msg = err.errors()[0]["msg"].removeprefix("Value error, ")
        if weekdays == 0:
            msg = "pick at least one day"
        raise HTTPException(422, msg) from err
    api.create_block(body, db, p)
    return _back(request)


@router.post("/schedule-blocks/{block_id}/delete")
def delete_block(request: Request, block_id: int, db: Session = Depends(get_db),
                 p: Principal = Depends(require_write)):
    api.delete_block(block_id, db, p)
    return _back(request)


@router.post("/overrides")
async def add_override(request: Request, db: Session = Depends(get_db),
                       p: Principal = Depends(require_write)):
    """One override per selected zone, or one for the chosen group or building."""
    form = await request.form()
    common = {
        "mode": form.get("mode", "custom"),
        "heat": float(form["heat"]) if form.get("heat") else None,
        "cool": float(form["cool"]) if form.get("cool") else None,
        "minutes": int(form.get("minutes") or 120),
        "reason": form.get("reason") or None,
    }
    targets = ([("group", int(form["group_id"]))] if form.get("group_id")
               else [("building", int(form["building_id"]))] if form.get("building_id")
               else [("zone", z) for z in sorted({int(z) for z in form.getlist("zone_ids")})])
    if not targets:
        raise HTTPException(400, "pick at least one zone, a group or a building")
    try:
        for kind, tid in targets:
            api.create_override(db, p, api.OverrideIn(target=kind, target_id=tid, **common))
    except ValidationError as err:
        raise HTTPException(422, err.errors()[0]["msg"]) from err
    return _back(request)


@router.post("/overrides/{override_id}/cancel")
def cancel(request: Request, override_id: int, db: Session = Depends(get_db),
           p: Principal = Depends(require_write)):
    api.cancel_override(override_id, db, p)
    return _back(request)


@router.get("/overrides", response_class=HTMLResponse)
def overrides_page(request: Request, db: Session = Depends(get_db),
                   p: Principal = Depends(current_principal)):
    rows = db.scalars(select(Override).where(Override.cancelled.is_(False),
                                             Override.ends_at > utcnow())
                      .order_by(Override.ends_at)).all()
    names = {("zone", z.id): z.name for z in db.scalars(select(Zone))}
    names |= {("group", g.id): g.name for g in db.scalars(select(ZoneGroup))}
    names |= {("thermostat", t.id): t.name for t in db.scalars(select(Thermostat))}
    cancellable = set()
    if p.can_write:
        for o in rows:
            kinds = [(k, getattr(o, f"{k}_id")) for k in ("thermostat", "zone", "group")]
            if all(p.may_touch_campus(c) for k, i in kinds
                   for c in api.target_campuses(db, k, i)):
                cancellable.add(o.id)
    return render(request, "overrides.html", p, rows=rows, names=names, cancellable=cancellable)


@router.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request, db: Session = Depends(get_db),
               p: Principal = Depends(current_principal)):
    rows = db.scalars(select(AuditLog).order_by(AuditLog.at.desc()).limit(200)).all()
    return render(request, "audit.html", p, rows=rows)


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db),
                  p: Principal = Depends(current_principal)):
    return _settings(request, db, p)


def _settings(request: Request, db: Session, p: Principal, new_key: dict | None = None):
    alert_rows = db.scalars(select(Alert).order_by(Alert.opened_at.desc()).limit(50)).all()
    admin = p.role == "admin"
    users = db.scalars(select(AppUser).order_by(AppUser.email)).all() if admin else []
    keys = [api.api_key_dict(k) for k in db.scalars(select(ApiKey).order_by(ApiKey.name))
            ] if admin else []
    return render(request, "settings.html", p, creds=db.get(EcobeeCredentials, 1),
                  syncs=db.scalars(select(CalendarSyncState)).all(),
                  google=gcal.configured(), message=request.query_params.get("msg"),
                  alert_rows=alert_rows, users=users, keys=keys, new_key=new_key,
                  all_campuses=db.scalars(select(Campus).order_by(Campus.name)).all())


@router.post("/settings/api-keys", response_class=HTMLResponse)
async def add_api_key(request: Request, db: Session = Depends(get_db),
                      p: Principal = Depends(require_admin)):
    form = await request.form()
    try:
        body = api.ApiKeyIn(name=form.get("name", "").strip(), role=form.get("role", "viewer"),
                            campus_ids=[int(x) for x in form.getlist("campus_ids")])
    except ValidationError as err:
        raise HTTPException(422, err.errors()[0]["msg"].removeprefix("Value error, ")) from err
    created = api.create_api_key(body, db, p)
    db.flush()
    # Rendered, not redirected: the key is shown once and never goes into a URL.
    return _settings(request, db, p, new_key=created)


@router.post("/settings/api-keys/{key_id}/delete")
def delete_api_key(key_id: int, db: Session = Depends(get_db),
                   p: Principal = Depends(require_admin)):
    api.revoke_api_key(key_id, db, p)
    return RedirectResponse("/settings?msg=API key revoked#api-keys", status_code=303)


@router.post("/settings/users/{user_id}")
async def save_user(request: Request, user_id: int, db: Session = Depends(get_db),
                    p: Principal = Depends(require_admin)):
    form = await request.form()
    try:
        body = api.UserIn(role=form.get("role"),
                          campus_ids=[int(x) for x in form.getlist("campus_ids")])
    except ValidationError as err:
        raise HTTPException(422, "unknown role") from err
    u = api.update_user(user_id, body, db, p)
    return RedirectResponse(f"/settings?msg=Saved {u['email']}", status_code=303)


@router.post("/settings/ecobee-login")
def ecobee_login(mfa_code: str = Form(""), p: Principal = Depends(require_admin)):
    try:
        get_backend().login(mfa_code or None)
        msg = "ecobee login succeeded"
    except (AuthRequired, BackendError) as err:
        record_auth_error(str(err))
        msg = f"ecobee login failed: {err}"
    return RedirectResponse(f"/settings?msg={msg}", status_code=303)


@router.post("/settings/run/{job}")
def run_job(request: Request, job: str, p: Principal = Depends(require_admin)):
    if job not in ("poll", "calendar", "reconcile", "alerts", "history"):
        raise HTTPException(404)
    result = api.run_job(job, p)
    return RedirectResponse(f"/settings?msg={job}: {result}", status_code=303)
