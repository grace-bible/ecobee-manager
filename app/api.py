"""REST API /api/v1 — the same operations the UI uses, for n8n / MCP / scripts."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app import alerts, gcal, history, poller, reconciler
from app.auth import (
    Principal,
    current_principal,
    hash_key,
    new_api_key,
    require_admin,
    require_write,
)
from app.config import get_settings
from app.db import get_db
from app.ecobee.backend import AuthRequired, BackendError
from app.insights import Point, daily_runtime, merge_room, occupancy, warmup_summary, warmups
from app.models import (
    Alert,
    ApiKey,
    AppUser,
    AuditLog,
    Campus,
    Override,
    ScheduleBlock,
    Thermostat,
    Zone,
    ZoneGroup,
    aware,
    utcnow,
)
from app.resolver import next_change, resolve
from app.services import get_backend, record_auth_error

router = APIRouter(prefix="/api/v1")
Mode = Literal["occupied", "unoccupied", "off", "custom"]


# ---------- schemas ----------

class CampusIn(BaseModel):
    name: str
    timezone: str = "America/Chicago"


class ZoneIn(BaseModel):
    name: str
    campus_id: int
    enabled: bool = True
    calendar_id: str | None = None
    calendar_name: str | None = None
    lead_minutes: int = Field(45, ge=0, le=240)
    tail_minutes: int = Field(15, ge=0, le=240)
    occupied_heat: float = 68
    occupied_cool: float = 72
    unoccupied_heat: float = 60
    unoccupied_cool: float = 82
    min_setpoint: float = Field(60, ge=45, le=92)
    max_setpoint: float = Field(85, ge=45, le=92)
    thermostat_ids: list[int] = []

    @model_validator(mode="after")
    def _check(self):
        if self.min_setpoint >= self.max_setpoint:
            raise ValueError("min_setpoint must be below max_setpoint")
        return self


class GroupIn(BaseModel):
    name: str
    description: str | None = None
    zone_ids: list[int] = []


class OverrideIn(BaseModel):
    target: Literal["thermostat", "zone", "group"]
    target_id: int
    mode: Mode = "custom"
    heat: float | None = None
    cool: float | None = None
    minutes: int | None = Field(None, ge=5, le=60 * 24 * 14)
    until: datetime | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def _check(self):
        if (self.minutes is None) == (self.until is None):
            raise ValueError("give exactly one of minutes or until")
        if self.mode == "custom" and self.heat is None and self.cool is None:
            raise ValueError("custom mode needs heat and/or cool")
        return self


class BlockIn(BaseModel):
    zone_id: int | None = None
    group_id: int | None = None
    weekdays: int = Field(..., ge=1, le=127, description="Mon=1 … Sun=64")
    start: str  # "HH:MM"
    end: str
    mode: Mode = "occupied"
    heat: float | None = None
    cool: float | None = None
    label: str | None = None

    @model_validator(mode="after")
    def _check(self):
        if (self.zone_id is None) == (self.group_id is None):
            raise ValueError("give exactly one of zone_id or group_id")
        for v in (self.start, self.end):
            try:
                time.fromisoformat(v)
            except ValueError as err:
                raise ValueError(f"times are HH:MM, got {v!r}") from err
        if self.start == self.end:
            raise ValueError("start and end must differ")
        if self.mode == "custom" and self.heat is None and self.cool is None:
            raise ValueError("custom mode needs heat and/or cool")
        return self


class UserIn(BaseModel):
    role: Literal["admin", "campus_manager", "viewer"]
    campus_ids: list[int] = []


class ApiKeyIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    role: Literal["admin", "campus_manager", "viewer"] = "viewer"
    campus_ids: list[int] = []

    @model_validator(mode="after")
    def _check(self):
        if self.role == "campus_manager" and not self.campus_ids:
            raise ValueError("a campus manager key needs at least one campus")
        return self


class LoginIn(BaseModel):
    mfa_code: str | None = None


# ---------- helpers ----------

def audit(db: Session, p: Principal, action: str, target: str, detail: dict | None = None):
    db.add(AuditLog(actor=p.name, source="api" if p.is_machine else "ui",
                    action=action, target=target, detail=detail))


def _zone_or_404(db: Session, zone_id: int) -> Zone:
    z = db.get(Zone, zone_id)
    if not z:
        raise HTTPException(404, "zone not found")
    return z


def _check_campus(p: Principal, campus_id: int | None):
    if not p.may_touch_campus(campus_id):
        raise HTTPException(403, "not allowed for this campus")


def target_campuses(db: Session, kind: str, target_id: int | None) -> list[int | None]:
    """Campus ids an action on this thermostat/zone/group touches (for campus managers)."""
    if target_id is None:
        return []
    obj = db.get({"thermostat": Thermostat, "zone": Zone, "group": ZoneGroup}[kind], target_id)
    if obj is None:
        raise HTTPException(404, f"{kind} not found")
    return [z.campus_id for z in obj.zones] if kind == "group" else [obj.campus_id]


def _check_target(db: Session, p: Principal, kind: str, target_id: int | None):
    for c in target_campuses(db, kind, target_id):
        _check_campus(p, c)


def block_dict(b: ScheduleBlock) -> dict:
    return {"id": b.id, "zone_id": b.zone_id, "group_id": b.group_id, "weekdays": b.weekdays,
            "start": b.start.strftime("%H:%M"), "end": b.end.strftime("%H:%M"), "mode": b.mode,
            "heat": b.heat, "cool": b.cool, "label": b.label}


def thermostat_dict(t: Thermostat) -> dict:
    return {
        "id": t.id, "identifier": t.identifier, "name": t.name, "model": t.model,
        "zone_id": t.zone_id, "connected": t.connected, "current_temp": t.current_temp,
        "humidity": t.humidity, "heat_setpoint": t.heat_setpoint, "cool_setpoint": t.cool_setpoint,
        "hvac_mode": t.hvac_mode, "equipment_running": t.equipment_running,
        "last_seen": t.last_seen, "app_hold": {
            "heat": t.app_hold_heat, "cool": t.app_hold_cool,
            "until": t.app_hold_until, "source": t.app_hold_source},
    }


def zone_dict(z: Zone) -> dict:
    return {c: getattr(z, c) for c in (
        "id", "name", "campus_id", "enabled", "calendar_id", "calendar_name", "lead_minutes",
        "tail_minutes", "occupied_heat", "occupied_cool", "unoccupied_heat", "unoccupied_cool",
        "min_setpoint", "max_setpoint")} | {"thermostat_ids": [t.id for t in z.thermostats]}


def _running(equipment: list[str]) -> str:
    """ecobee equipmentStatus ('heatPump,fan', 'compCool1', …) → heating/cooling/fan/idle."""
    blob = ",".join(e for e in equipment if e).lower()
    if "heat" in blob:
        return "heating"
    if "cool" in blob:
        return "cooling"
    if "fan" in blob:
        return "fan"
    return "idle"


def zone_status(db: Session, z: Zone, now: datetime | None = None,
                look_ahead: timedelta = timedelta(hours=24)) -> dict:
    """What a zone is doing now and what it does next, worded for whoever is in the room.
    Zone-level only: per-thermostat overrides show on the thermostat, not here."""
    now = now or utcnow()
    cfg = reconciler.zone_cfg(z)
    deadband = get_settings().min_deadband_f
    zone_ovs, _, events, blocks = reconciler.gather_inputs(db, z, now, look_ahead)
    d = resolve(cfg, now, zone_ovs, events, blocks, deadband)
    until = next_change(cfg, now, zone_ovs, events, blocks, horizon=look_ahead)
    nxt = (resolve(cfg, until, zone_ovs, events, blocks, deadband)
           if until < now + look_ahead else None)

    override = event = None
    kind, _, ref_id = (d.ref or "").partition(":")
    if kind == "override":
        o = db.get(Override, int(ref_id))
        if o:
            override = {"id": o.id, "scope": "zone" if o.zone_id == z.id else "group",
                        "group": (g.name if o.group_id and (g := db.get(ZoneGroup, o.group_id))
                                  else None),
                        "created_by": o.created_by, "reason": o.reason, "ends_at": o.ends_at}
    elif kind == "event":
        event = next((e.title for e in events if e.id == ref_id), None) or "a booking"

    online = [t for t in z.thermostats if t.connected]
    temps = [t.current_temp for t in online if t.current_temp is not None]
    return {
        "zone_id": z.id, "zone": z.name, "campus_id": z.campus_id, "enabled": z.enabled,
        "temp": round(sum(temps) / len(temps), 1) if temps else None,
        "humidity": next((t.humidity for t in online if t.humidity is not None), None),
        "running": _running([t.equipment_running or "" for t in online]),
        "thermostats": len(z.thermostats), "online": len(online),
        "heat": d.heat, "cool": d.cool, "intent": d.intent, "source": d.source,
        "override": override, "event": event, "until": until if nxt else None,
        "next": {"heat": nxt.heat, "cool": nxt.cool, "intent": nxt.intent,
                 "source": nxt.source} if nxt else None,
    }


def room_points(z: Zone, since: datetime, until: datetime | None = None) -> list[Point]:
    """The room's 5-minute history (all its thermostats merged)."""
    ids = [t.id for t in z.thermostats]
    by_t: dict[int, list[Point]] = {i: [] for i in ids}
    for r in (history.samples(ids, since, until) if ids else []):
        by_t[r.thermostat_id].append(Point(aware(r.at), r.temp, r.heat_sp, r.cool_sp, r.outdoor,
                                           r.heat_s, r.cool_s, r.occupied))
    return merge_room(list(by_t.values()))


def room_insights(z: Zone, now: datetime | None = None, days: int = 7) -> dict:
    """Warm-up times, daily heating/cooling hours and occupancy over the last `days`."""
    now = now or utcnow()
    tz = z.campus.timezone if z.campus else get_settings().default_timezone
    pts = room_points(z, now - timedelta(days=days))
    ws = warmups(pts)
    return {
        "zone_id": z.id, "days": days, "samples": len(pts),
        "last_sample": pts[-1].at if pts else None,
        "lead_minutes": z.lead_minutes,
        "warmups": [w.__dict__ for w in ws[-12:]],
        "warmup_summary": warmup_summary(ws),
        "runtime": [{"day": d, "heat_h": h, "cool_h": c} for d, h, c in daily_runtime(pts, tz)],
        "runtime_total": {"heat_h": round(sum(p.heat_s for p in pts) / 3600, 1),
                          "cool_h": round(sum(p.cool_s for p in pts) / 3600, 1)},
        "occupancy": occupancy(pts, z.unoccupied_heat, z.unoccupied_cool),
    }


# ---------- reads ----------

@router.get("/me")
def me(p: Principal = Depends(current_principal)):
    return p.__dict__


@router.get("/campuses")
def list_campuses(db: Session = Depends(get_db), _=Depends(current_principal)):
    return [{"id": c.id, "name": c.name, "timezone": c.timezone}
            for c in db.scalars(select(Campus).order_by(Campus.name))]


@router.get("/thermostats")
def list_thermostats(db: Session = Depends(get_db), _=Depends(current_principal)):
    return [thermostat_dict(t) for t in db.scalars(select(Thermostat).order_by(Thermostat.name))]


@router.get("/zones")
def list_zones(db: Session = Depends(get_db), _=Depends(current_principal)):
    zs = db.scalars(select(Zone).options(selectinload(Zone.thermostats)).order_by(Zone.name))
    return [zone_dict(z) for z in zs]


@router.get("/zones/{zone_id}/status")
def get_zone_status(zone_id: int, db: Session = Depends(get_db), _=Depends(current_principal)):
    """What the room is doing now and next (target, why, until when)."""
    z = db.get(Zone, zone_id)
    if not z:
        raise HTTPException(404, "zone not found")
    return zone_status(db, z)


@router.get("/zones/{zone_id}/history")
def get_zone_history(zone_id: int, hours: int = 24, db: Session = Depends(get_db),
                     _=Depends(current_principal)):
    """5-minute history for the room (≤ 31 days): temp, setpoints, outdoor, runtime."""
    z = _zone_or_404(db, zone_id)
    pts = room_points(z, utcnow() - timedelta(hours=min(max(hours, 1), 24 * 31)))
    return [p.__dict__ for p in pts]


@router.get("/zones/{zone_id}/insights")
def get_zone_insights(zone_id: int, days: int = 7, db: Session = Depends(get_db),
                      _=Depends(current_principal)):
    return room_insights(_zone_or_404(db, zone_id), days=min(max(days, 1), 31))


@router.get("/groups")
def list_groups(db: Session = Depends(get_db), _=Depends(current_principal)):
    return [{"id": g.id, "name": g.name, "description": g.description,
             "zone_ids": [z.id for z in g.zones]}
            for g in db.scalars(select(ZoneGroup).order_by(ZoneGroup.name))]


@router.get("/schedule-blocks")
def list_blocks(zone_id: int | None = None, group_id: int | None = None,
                db: Session = Depends(get_db), _=Depends(current_principal)):
    q = select(ScheduleBlock).order_by(ScheduleBlock.start)
    if zone_id is not None:
        q = q.where(ScheduleBlock.zone_id == zone_id)
    if group_id is not None:
        q = q.where(ScheduleBlock.group_id == group_id)
    return [block_dict(b) for b in db.scalars(q)]


@router.get("/alerts")
def list_alerts(include_resolved: bool = False, db: Session = Depends(get_db),
                _=Depends(current_principal)):
    q = select(Alert).order_by(Alert.opened_at.desc()).limit(200)
    if not include_resolved:
        q = q.where(Alert.resolved_at.is_(None))
    return [{"id": a.id, "key": a.key, "kind": a.kind, "message": a.message,
             "opened_at": a.opened_at, "resolved_at": a.resolved_at} for a in db.scalars(q)]


@router.get("/users")
def list_users(db: Session = Depends(get_db), _=Depends(require_admin)):
    return [{"id": u.id, "email": u.email, "role": u.role, "campus_ids": u.campus_ids or [],
             "last_login": u.last_login} for u in db.scalars(select(AppUser).order_by(AppUser.email))]


@router.get("/plan")
def get_plan(_=Depends(current_principal)):
    """What the reconciler would do right now, per thermostat."""
    return reconciler.plan()


@router.get("/calendars/resources")
def calendar_resources(_=Depends(current_principal)):
    if not gcal.configured():
        raise HTTPException(503, "Google service account not configured")
    return [r.__dict__ for r in gcal.list_resources()]


@router.get("/overrides")
def list_overrides(db: Session = Depends(get_db), _=Depends(current_principal)):
    rows = db.scalars(select(Override).where(Override.cancelled.is_(False),
                                             Override.ends_at > utcnow()))
    return [{c: getattr(o, c) for c in ("id", "thermostat_id", "zone_id", "group_id", "mode",
                                        "heat", "cool", "starts_at", "ends_at", "created_by",
                                        "reason")} for o in rows]


@router.get("/audit")
def list_audit(limit: int = 100, db: Session = Depends(get_db), _=Depends(current_principal)):
    rows = db.scalars(select(AuditLog).order_by(AuditLog.at.desc()).limit(min(limit, 500)))
    return [{"at": a.at, "actor": a.actor, "source": a.source, "action": a.action,
             "target": a.target, "detail": a.detail} for a in rows]


# ---------- writes ----------

@router.post("/campuses", status_code=201)
def create_campus(body: CampusIn, db: Session = Depends(get_db), p=Depends(require_admin)):
    c = Campus(name=body.name, timezone=body.timezone)
    db.add(c)
    db.flush()
    audit(db, p, "create_campus", c.name)
    return {"id": c.id}


def _apply_zone(db: Session, z: Zone, body: ZoneIn):
    for k, v in body.model_dump(exclude={"thermostat_ids"}).items():
        setattr(z, k, v)
    if body.thermostat_ids is not None:
        for t in list(z.thermostats):
            if t.id not in body.thermostat_ids:
                t.zone_id = None
        for tid in body.thermostat_ids:
            t = db.get(Thermostat, tid)
            if not t:
                raise HTTPException(400, f"thermostat {tid} not found")
            t.zone, t.campus_id = z, body.campus_id


@router.post("/zones", status_code=201)
def create_zone(body: ZoneIn, db: Session = Depends(get_db), p=Depends(require_write)):
    _check_campus(p, body.campus_id)
    z = Zone(name=body.name, campus_id=body.campus_id)
    db.add(z)
    _apply_zone(db, z, body)
    db.flush()
    audit(db, p, "create_zone", z.name, body.model_dump())
    return {"id": z.id}


@router.put("/zones/{zone_id}")
def update_zone(zone_id: int, body: ZoneIn, db: Session = Depends(get_db),
                p=Depends(require_write)):
    z = _zone_or_404(db, zone_id)
    _check_campus(p, z.campus_id)
    _check_campus(p, body.campus_id)
    _apply_zone(db, z, body)
    audit(db, p, "update_zone", z.name, body.model_dump())
    return zone_dict(z)


@router.delete("/zones/{zone_id}")
def delete_zone(zone_id: int, db: Session = Depends(get_db), p=Depends(require_write)):
    z = _zone_or_404(db, zone_id)
    _check_campus(p, z.campus_id)
    for t in list(z.thermostats):
        t.zone_id = None  # they fall back to their own program as the last hold expires
    db.delete(z)
    audit(db, p, "delete_zone", z.name)
    return {"ok": True}


def _group_or_404(db: Session, group_id: int) -> ZoneGroup:
    g = db.get(ZoneGroup, group_id)
    if not g:
        raise HTTPException(404, "group not found")
    return g


def _set_group_zones(db: Session, p: Principal, g: ZoneGroup, zone_ids: list[int]):
    new = [_zone_or_404(db, zid) for zid in zone_ids]
    # A campus manager may only add or remove zones on their own campuses.
    for z in set(new) ^ set(g.zones):
        _check_campus(p, z.campus_id)
    g.zones = new


@router.post("/groups", status_code=201)
def create_group(body: GroupIn, db: Session = Depends(get_db), p=Depends(require_write)):
    g = ZoneGroup(name=body.name, description=body.description)
    _set_group_zones(db, p, g, body.zone_ids)
    db.add(g)
    db.flush()
    audit(db, p, "create_group", g.name, body.model_dump())
    return {"id": g.id}


@router.put("/groups/{group_id}")
def update_group(group_id: int, body: GroupIn, db: Session = Depends(get_db),
                 p=Depends(require_write)):
    g = _group_or_404(db, group_id)
    g.name, g.description = body.name, body.description
    _set_group_zones(db, p, g, body.zone_ids)
    audit(db, p, "update_group", g.name, body.model_dump())
    return {"id": g.id, "name": g.name, "zone_ids": [z.id for z in g.zones]}


@router.delete("/groups/{group_id}")
def delete_group(group_id: int, db: Session = Depends(get_db), p=Depends(require_write)):
    g = _group_or_404(db, group_id)
    for z in g.zones:
        _check_campus(p, z.campus_id)
    db.delete(g)
    audit(db, p, "delete_group", g.name)
    return {"ok": True}


@router.post("/schedule-blocks", status_code=201)
def create_block(body: BlockIn, db: Session = Depends(get_db), p=Depends(require_write)):
    if body.zone_id is not None:
        _check_target(db, p, "zone", body.zone_id)
    else:
        _check_target(db, p, "group", body.group_id)
    b = ScheduleBlock(zone_id=body.zone_id, group_id=body.group_id, weekdays=body.weekdays,
                      start=time.fromisoformat(body.start), end=time.fromisoformat(body.end),
                      mode=body.mode, heat=body.heat, cool=body.cool, label=body.label)
    db.add(b)
    db.flush()
    audit(db, p, "create_schedule_block", body.label or f"block {b.id}", body.model_dump())
    return {"id": b.id}


@router.delete("/schedule-blocks/{block_id}")
def delete_block(block_id: int, db: Session = Depends(get_db), p=Depends(require_write)):
    b = db.get(ScheduleBlock, block_id)
    if not b:
        raise HTTPException(404, "schedule block not found")
    if b.zone_id is not None:
        _check_target(db, p, "zone", b.zone_id)
    else:
        _check_target(db, p, "group", b.group_id)
    db.delete(b)
    audit(db, p, "delete_schedule_block", b.label or f"block {b.id}", block_dict(b))
    return {"ok": True}


def create_override(db: Session, p: Principal, body: OverrideIn) -> Override:
    model = {"thermostat": Thermostat, "zone": Zone, "group": ZoneGroup}[body.target]
    target = db.get(model, body.target_id)
    if not target:
        raise HTTPException(404, f"{body.target} not found")
    _check_target(db, p, body.target, body.target_id)
    now = utcnow()
    ends = body.until or now + timedelta(minutes=body.minutes)
    if ends <= now:
        raise HTTPException(400, "override must end in the future")
    o = Override(mode=body.mode, heat=body.heat, cool=body.cool, starts_at=now, ends_at=ends,
                 created_by=p.name, reason=body.reason,
                 **{f"{body.target}_id": body.target_id})
    db.add(o)
    db.flush()
    audit(db, p, "create_override", f"{body.target}:{target.name}", body.model_dump(mode="json"))
    return o


@router.post("/overrides", status_code=201)
def post_override(body: OverrideIn, db: Session = Depends(get_db), p=Depends(require_write)):
    return {"id": create_override(db, p, body).id}


@router.delete("/overrides/{override_id}")
def cancel_override(override_id: int, db: Session = Depends(get_db), p=Depends(require_write)):
    o = db.get(Override, override_id)
    if not o:
        raise HTTPException(404, "override not found")
    for kind in ("thermostat", "zone", "group"):
        _check_target(db, p, kind, getattr(o, f"{kind}_id"))
    o.cancelled = True
    audit(db, p, "cancel_override", f"override:{o.id}")
    return {"ok": True}


# ---------- admin ----------

@router.post("/ecobee/login")
def ecobee_login(body: LoginIn, p=Depends(require_admin)):
    try:
        get_backend().login(body.mfa_code)
    except AuthRequired as err:
        record_auth_error(str(err))
        raise HTTPException(401, str(err)) from err
    except BackendError as err:
        record_auth_error(str(err))
        raise HTTPException(502, str(err)) from err
    return {"ok": True}


@router.post("/jobs/{job}")
def run_job(job: Literal["poll", "calendar", "reconcile", "alerts", "history"],
            p=Depends(require_admin)):
    fn = {"poll": poller.poll_once, "calendar": gcal.sync_all,
          "reconcile": reconciler.reconcile_once, "alerts": alerts.check_once,
          "history": history.sync_once}[job]
    return fn()


@router.put("/users/{user_id}")
def update_user(user_id: int, body: UserIn, db: Session = Depends(get_db),
                p=Depends(require_admin)):
    u = db.get(AppUser, user_id)
    if not u:
        raise HTTPException(404, "user not found")
    _check_campuses(db, body.campus_ids)
    u.role = body.role
    u.campus_ids = body.campus_ids if body.role == "campus_manager" else []
    audit(db, p, "update_user", u.email, body.model_dump())
    return {"id": u.id, "email": u.email, "role": u.role, "campus_ids": u.campus_ids}


def _check_campuses(db: Session, campus_ids: list[int]) -> None:
    for cid in campus_ids:
        if not db.get(Campus, cid):
            raise HTTPException(400, f"campus {cid} not found")


def api_key_dict(k: ApiKey) -> dict:
    return {"id": k.id, "name": k.name, "role": k.role, "campus_ids": k.campus_ids or [],
            "created_at": k.created_at}


@router.get("/api-keys")
def list_api_keys(db: Session = Depends(get_db), _=Depends(require_admin)):
    return [api_key_dict(k) for k in db.scalars(select(ApiKey).order_by(ApiKey.name))]


@router.post("/api-keys", status_code=201)
def create_api_key(body: ApiKeyIn, db: Session = Depends(get_db), p=Depends(require_admin)):
    _check_campuses(db, body.campus_ids)
    if db.scalar(select(ApiKey).where(ApiKey.name == body.name)):
        raise HTTPException(409, f"an API key named {body.name!r} already exists")
    raw = new_api_key()
    campus_ids = body.campus_ids if body.role == "campus_manager" else []
    db.add(ApiKey(name=body.name, key_hash=hash_key(raw), role=body.role, campus_ids=campus_ids))
    audit(db, p, "create_api_key", body.name, {"role": body.role, "campus_ids": campus_ids})
    return {"name": body.name, "key": raw, "role": body.role, "campus_ids": campus_ids,
            "note": "shown once; store it now"}


@router.delete("/api-keys/{key_id}")
def revoke_api_key(key_id: int, db: Session = Depends(get_db), p=Depends(require_admin)):
    k = db.get(ApiKey, key_id)
    if not k:
        raise HTTPException(404, "API key not found")
    db.delete(k)
    audit(db, p, "revoke_api_key", k.name)
    return {"ok": True}
