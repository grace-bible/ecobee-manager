"""Server-rendered UI (Jinja + HTMX). Mutations reuse the API's validation and helpers."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app import alerts, api, gcal, reconciler
from app.auth import Principal, current_principal, require_admin, require_write
from app.config import get_settings
from app.db import get_db
from app.ecobee.backend import AuthRequired, BackendError
from app.models import (
    Alert,
    AppUser,
    AuditLog,
    CalendarEvent,
    CalendarSyncState,
    Campus,
    EcobeeCredentials,
    Override,
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


templates.env.filters["local"] = _fmt_local
templates.env.globals["settings"] = get_settings
templates.env.globals["open_alerts"] = alerts.open_alerts


def render(request: Request, name: str, p: Principal, **ctx):
    return templates.TemplateResponse(request, name, {"me": p, **ctx})


def _back(request: Request, fallback: str = "/") -> RedirectResponse:
    return RedirectResponse(request.headers.get("referer") or fallback, status_code=303)


@router.get("/", response_class=HTMLResponse)
def overview(request: Request, db: Session = Depends(get_db),
             p: Principal = Depends(current_principal)):
    campuses = db.scalars(select(Campus).order_by(Campus.name).options(
        selectinload(Campus.zones).selectinload(Zone.thermostats))).all()
    unassigned = db.scalars(select(Thermostat).where(Thermostat.zone_id.is_(None))
                            .order_by(Thermostat.name)).all()
    plan = {row["thermostat_id"]: row for row in reconciler.plan()}
    groups = db.scalars(select(ZoneGroup).order_by(ZoneGroup.name)
                        .options(selectinload(ZoneGroup.zones))).all()
    touchable = [c.id for c in campuses if p.may_touch_campus(c.id)]
    return render(request, "overview.html", p, campuses=campuses, unassigned=unassigned,
                  plan=plan, groups=groups, touchable=touchable)


@router.get("/zones/new", response_class=HTMLResponse)
def new_zone_page(request: Request, db: Session = Depends(get_db),
                  p: Principal = Depends(current_principal)):
    return zone_page(request, None, db, p)


@router.get("/zones/{zone_id}", response_class=HTMLResponse)
def zone_page(request: Request, zone_id: int | None, db: Session = Depends(get_db),
              p: Principal = Depends(current_principal)):
    zone = db.get(Zone, zone_id) if zone_id else None
    if zone_id and not zone:
        raise HTTPException(404)
    resources, resource_error = [], None
    if gcal.configured():
        try:
            resources = gcal.list_resources()
        except Exception as err:  # show the page anyway; the id can be typed in
            resource_error = str(err)
    events, preview = [], []
    if zone:
        now = utcnow()
        if zone.calendar_id:
            events = db.scalars(select(CalendarEvent).where(
                CalendarEvent.calendar_id == zone.calendar_id,
                CalendarEvent.ends_at > now).order_by(CalendarEvent.starts_at).limit(15)).all()
        preview = _preview(db, zone, now)
    thermostats = db.scalars(select(Thermostat).where(
        (Thermostat.zone_id.is_(None)) | (Thermostat.zone_id == zone_id))
        .order_by(Thermostat.name)).all()
    blocks = (db.scalars(select(ScheduleBlock).where(ScheduleBlock.zone_id == zone.id)
                         .order_by(ScheduleBlock.start)).all() if zone else [])
    editable = p.can_write and (zone is None or p.may_touch_campus(zone.campus_id))
    return render(request, "zone.html", p, zone=zone,
                  campuses=db.scalars(select(Campus).order_by(Campus.name)).all(),
                  thermostats=thermostats, resources=resources, resource_error=resource_error,
                  events=events, preview=preview, blocks=[api.block_dict(b) for b in blocks],
                  editable=editable,
                  sync=db.get(CalendarSyncState, zone.calendar_id) if zone and zone.calendar_id else None)


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
    data = {k: v for k, v in form.items() if k != "thermostat_ids" and v != ""}
    data["enabled"] = "enabled" in form
    data["thermostat_ids"] = [int(x) for x in form.getlist("thermostat_ids")]
    if data.get("calendar_id") and not data.get("calendar_name"):
        data["calendar_name"] = data["calendar_id"]
    try:
        body = api.ZoneIn(**data)
    except ValidationError as err:
        raise HTTPException(422, err.errors()[0]["msg"]) from err
    if zone_id:
        api.update_zone(zone_id, body, db, p)
    else:
        zone_id = api.create_zone(body, db, p)["id"]
    return RedirectResponse(f"/zones/{zone_id}", status_code=303)


@router.post("/campuses")
def add_campus(name: str = Form(...), timezone: str = Form("America/Chicago"),
               db: Session = Depends(get_db), p: Principal = Depends(require_admin)):
    api.create_campus(api.CampusIn(name=name, timezone=timezone), db, p)
    return RedirectResponse("/", status_code=303)


@router.post("/zones/{zone_id}/delete")
def delete_zone(zone_id: int, db: Session = Depends(get_db),
                p: Principal = Depends(require_write)):
    api.delete_zone(zone_id, db, p)
    return RedirectResponse("/", status_code=303)


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
    g = db.get(ZoneGroup, group_id)
    if not g:
        raise HTTPException(404)
    blocks = db.scalars(select(ScheduleBlock).where(ScheduleBlock.group_id == g.id)
                        .order_by(ScheduleBlock.start)).all()
    editable = p.can_write and all(p.may_touch_campus(z.campus_id) for z in g.zones)
    return render(request, "group.html", p, group=g, campuses=_campuses_with_zones(db),
                  blocks=[api.block_dict(b) for b in blocks], editable=editable)


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
    return RedirectResponse(f"/groups/{gid}", status_code=303)


@router.post("/groups/{group_id}")
async def save_group(request: Request, group_id: int, db: Session = Depends(get_db),
                     p: Principal = Depends(require_write)):
    api.update_group(group_id, _group_body(await request.form()), db, p)
    return RedirectResponse(f"/groups/{group_id}", status_code=303)


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
    data = {k: form.get(k) or None for k in
            ("zone_id", "group_id", "start", "end", "mode", "heat", "cool", "label")}
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
    """Bulk bar: one override per selected zone, or one for the chosen group."""
    form = await request.form()
    common = {
        "mode": form.get("mode", "custom"),
        "heat": float(form["heat"]) if form.get("heat") else None,
        "cool": float(form["cool"]) if form.get("cool") else None,
        "minutes": int(form.get("minutes") or 120),
        "reason": form.get("reason") or None,
    }
    targets = ([("group", int(form["group_id"]))] if form.get("group_id")
               else [("zone", z) for z in sorted({int(z) for z in form.getlist("zone_ids")})])
    if not targets:
        raise HTTPException(400, "pick at least one zone or a group")
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
    alert_rows = db.scalars(select(Alert).order_by(Alert.opened_at.desc()).limit(50)).all()
    users = (db.scalars(select(AppUser).order_by(AppUser.email)).all()
             if p.role == "admin" else [])
    return render(request, "settings.html", p, creds=db.get(EcobeeCredentials, 1),
                  syncs=db.scalars(select(CalendarSyncState)).all(),
                  google=gcal.configured(), message=request.query_params.get("msg"),
                  alert_rows=alert_rows, users=users,
                  all_campuses=db.scalars(select(Campus).order_by(Campus.name)).all())


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
    if job not in ("poll", "calendar", "reconcile", "alerts"):
        raise HTTPException(404)
    result = api.run_job(job, p)
    return RedirectResponse(f"/settings?msg={job}: {result}", status_code=303)
