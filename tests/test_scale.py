"""Scale settings: comfort profiles, seasonal blocks, room booking rules, learned lead,
week view."""

from datetime import timedelta

from fastapi.testclient import TestClient

from app import history, lead, poller
from app.db import session_scope
from app.main import app
from app.models import CalendarEvent, utcnow


def _campus_zone(c, name, therm="Office Suite"):
    poller.poll_once()
    therms = {t["name"]: t for t in c.get("/api/v1/thermostats").json()}
    cid = c.post("/api/v1/campuses", json={"name": name}).json()["id"]
    zid = c.post("/api/v1/zones", json={"name": f"{name} zone", "campus_id": cid,
                                        "thermostat_ids": [therms[therm]["id"]]}).json()["id"]
    return cid, zid


def _zone(c, zid):
    z = next(x for x in c.get("/api/v1/zones").json() if x["id"] == zid)
    for k in ("id", "calendar_ids"):
        z.pop(k)
    return z


def test_comfort_profile_owns_zone_settings():
    with TestClient(app) as c:
        _, zid = _campus_zone(c, "Profiles")
        pid = c.post("/api/v1/comfort-profiles", json={
            "name": "Offices", "occupied_heat": 70, "occupied_cool": 74,
            "lead_minutes": 30}).json()["id"]
        z = _zone(c, zid) | {"profile_id": pid, "occupied_heat": 50}
        r = c.put(f"/api/v1/zones/{zid}", json=z).json()
        assert (r["occupied_heat"], r["occupied_cool"], r["lead_minutes"]) == (70, 74, 30)
        # Editing the profile updates every zone that uses it.
        upd = c.put(f"/api/v1/comfort-profiles/{pid}", json={
            "name": "Offices", "occupied_heat": 69, "occupied_cool": 73, "lead_minutes": 30})
        assert upd.json()["zone_ids"] == [zid]
        assert _zone(c, zid)["occupied_heat"] == 69
        # Removing it leaves the zone on its last values.
        assert c.delete(f"/api/v1/comfort-profiles/{pid}").json() == {"ok": True}
        z = _zone(c, zid)
        assert z["profile_id"] is None and z["occupied_heat"] == 69
        # A campus-only profile can't be used on another campus.
        other = c.post("/api/v1/campuses", json={"name": "Profiles 2"}).json()["id"]
        p2 = c.post("/api/v1/comfort-profiles", json={"name": "P2 only",
                                                      "campus_id": other}).json()["id"]
        assert c.put(f"/api/v1/zones/{zid}", json=z | {"profile_id": p2}).status_code == 400
        assert c.get("/setup/profiles").status_code == 200


def test_seasonal_block_from_the_form():
    with TestClient(app) as c:
        _, zid = _campus_zone(c, "Seasons", therm="Kids Wing 101")
        r = c.post("/schedule-blocks", data={
            "zone_id": zid, "weekdays": ["1"], "start": "09:00", "end": "12:00",
            "mode": "occupied", "season_start": "2026-06-01", "season_end": "2026-08-14"},
            headers={"referer": f"/zones/{zid}"}, follow_redirects=False)
        assert r.status_code == 303
        b = c.get(f"/api/v1/schedule-blocks?zone_id={zid}").json()[0]
        assert (b["season_start"], b["season_end"]) == ("06-01", "08-14")
        assert "Jun 1–Aug 14 each year" in c.get(f"/zones/{zid}").text
        bad = c.post("/api/v1/schedule-blocks", json={
            "zone_id": zid, "weekdays": 1, "start": "09:00", "end": "10:00",
            "season_start": "06-01"})
        assert bad.status_code == 422


def test_room_booking_rules():
    with TestClient(app) as c:
        cid, zid = _campus_zone(c, "Rules", therm="Kids Wing 102")
        rid = c.post("/api/v1/rooms", json={"name": "Closet", "campus_id": cid,
                                            "calendar_id": "rules-closet",
                                            "zone_ids": [zid]}).json()["id"]
        now = utcnow()
        with session_scope() as s:
            s.add(CalendarEvent(calendar_id="rules-closet", event_id="short", title="Quick",
                                starts_at=now - timedelta(minutes=1),
                                ends_at=now + timedelta(minutes=9)))
        assert c.get(f"/api/v1/zones/{zid}/status").json()["source"] == "calendar"
        room = next(r for r in c.get("/api/v1/rooms").json() if r["id"] == rid)
        base = {k: room[k] for k in ("name", "campus_id", "calendar_id")}
        c.put(f"/api/v1/rooms/{rid}", json=base | {"min_booking_minutes": 15})
        assert c.get(f"/api/v1/zones/{zid}/status").json()["source"] != "calendar"
        c.put(f"/api/v1/rooms/{rid}", json=base | {"drives_hvac": False})
        assert c.get(f"/api/v1/zones/{zid}/status").json()["source"] != "calendar"


def test_learned_lead_job_and_week_view():
    with TestClient(app) as c:
        cid, zid = _campus_zone(c, "Learn", therm="Fellowship Hall")
        history.sync_once()
        c.put(f"/api/v1/zones/{zid}", json=_zone(c, zid) | {"lead_auto": True})
        r = lead.learn_once()
        assert r["zones"] >= 1
        z = _zone(c, zid)
        assert z["lead_auto"] is True and 15 <= z["lead_minutes"] <= 180
        bld = c.post("/api/v1/buildings", json={"name": "Hall", "campus_id": cid}).json()["id"]
        c.put(f"/api/v1/zones/{zid}", json=_zone(c, zid) | {"building_id": bld})
        page = c.get(f"/buildings/{bld}/week").text
        assert "Week of" in page and page.count('class="week-day"') == 7
        assert c.get(f"/zones/{zid}/week?week=2026-12-21").status_code == 200
