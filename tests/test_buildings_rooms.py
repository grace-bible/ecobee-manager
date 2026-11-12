"""Campus → building → zone ↔ room: several room calendars drive one zone; buildings can be
overridden and scheduled; Google import; permissions."""

from datetime import timedelta

from fastapi.testclient import TestClient

from app import api, poller, reconciler
from app.auth import Principal
from app.db import session_scope
from app.gcal import Resource
from app.main import app
from app.models import CalendarEvent, utcnow


def _setup(c, name):
    poller.poll_once()
    therms = {t["name"]: t for t in c.get("/api/v1/thermostats").json()}
    campus = c.post("/api/v1/campuses", json={"name": name}).json()["id"]
    bld = c.post("/api/v1/buildings", json={"name": "Education", "campus_id": campus}).json()["id"]
    r1 = c.post("/api/v1/rooms", json={"name": "Kids 101", "campus_id": campus,
                                       "building_id": bld, "calendar_id": f"{name}-101"})
    r2 = c.post("/api/v1/rooms", json={"name": "Kids 102", "campus_id": campus,
                                       "building_id": bld, "calendar_id": f"{name}-102"})
    zone = c.post("/api/v1/zones", json={
        "name": "Kids west", "campus_id": campus, "building_id": bld,
        "room_ids": [r1.json()["id"], r2.json()["id"]],
        "thermostat_ids": [therms["Kids Wing 101"]["id"]]}).json()["id"]
    return campus, bld, [r1.json()["id"], r2.json()["id"]], zone


def _book(cal, event_id, title, start, hours=1):
    with session_scope() as s:
        s.add(CalendarEvent(calendar_id=cal, event_id=event_id, title=title,
                            starts_at=start, ends_at=start + timedelta(hours=hours)))


def test_any_room_booking_drives_the_zone_once():
    with TestClient(app) as c:
        _, _, _, zone = _setup(c, "Rooms1")
        z = c.get("/api/v1/zones").json()
        assert next(x for x in z if x["id"] == zone)["calendar_ids"] == ["Rooms1-101",
                                                                          "Rooms1-102"]
        now = utcnow()
        # Same event booked in both rooms (shared id), and the zone is "booked" now.
        _book("Rooms1-102", "evt-shared", "Youth night", now - timedelta(minutes=5))
        _book("Rooms1-101", "evt-shared", "Youth night", now - timedelta(minutes=5))
        s = c.get(f"/api/v1/zones/{zone}/status").json()
        assert (s["source"], s["event"], s["intent"]) == ("calendar", "Youth night", "occupied")
        assert s["event_room"] in ("Kids 101", "Kids 102") and s["rooms"] == ["Kids 101",
                                                                              "Kids 102"]
        page = c.get(f"/zones/{zone}").text
        assert "Youth night" in page and "Serves Kids 101, Kids 102" in page


def test_building_override_and_schedule_apply_below_zone():
    with TestClient(app) as c:
        _, bld, _, zone = _setup(c, "Rooms2")
        # Building schedule all week → zone follows it…
        r = c.post("/api/v1/schedule-blocks", json={"building_id": bld, "weekdays": 127,
                                                    "start": "00:00", "end": "23:59",
                                                    "mode": "occupied", "label": "All day"})
        assert r.status_code == 201
        assert c.get(f"/api/v1/zones/{zone}/status").json()["source"] == "schedule"
        # …a building override beats the schedule…
        o = c.post("/api/v1/overrides", json={"target": "building", "target_id": bld,
                                              "mode": "off", "minutes": 30}).json()["id"]
        s = c.get(f"/api/v1/zones/{zone}/status").json()
        assert (s["intent"], s["override"]["scope"], s["override"]["group"]) == (
            "off", "building", "Education")
        # …and a zone override beats the building one.
        c.post("/api/v1/overrides", json={"target": "zone", "target_id": zone,
                                          "mode": "custom", "heat": 70, "cool": 74,
                                          "minutes": 30})
        assert c.get(f"/api/v1/zones/{zone}/status").json()["override"]["scope"] == "zone"
        c.delete(f"/api/v1/overrides/{o}")
        assert c.get(f"/buildings/{bld}").status_code == 200
        assert "All day" in c.get(f"/buildings/{bld}").text


def test_room_rules_and_permissions():
    with TestClient(app) as c:
        campus, bld, _, zone = _setup(c, "Rooms3")
        other = c.post("/api/v1/campuses", json={"name": "Rooms3b"}).json()["id"]
        dup = c.post("/api/v1/rooms", json={"name": "Dup", "campus_id": campus,
                                            "calendar_id": "Rooms3-101"})
        assert dup.status_code == 409
        cross = c.post("/api/v1/rooms", json={"name": "X", "campus_id": other,
                                              "zone_ids": [zone]})
        assert cross.status_code == 400
        assert c.post("/api/v1/zones", json={"name": "Bad", "campus_id": other,
                                             "building_id": bld}).status_code == 400

        mgr = c.post("/api/v1/api-keys", json={"name": "rooms3-mgr", "role": "campus_manager",
                                               "campus_ids": [other]}).json()["key"]
        h = {"authorization": f"Bearer {mgr}"}
        assert c.post("/api/v1/rooms", headers=h, json={"name": "Nope", "campus_id": campus}
                      ).status_code == 403
        assert c.post("/api/v1/overrides", headers=h, json={
            "target": "building", "target_id": bld, "mode": "off", "minutes": 30}
            ).status_code == 403

        # Deleting a building keeps its zones and rooms.
        assert c.delete(f"/api/v1/buildings/{bld}").json() == {"ok": True}
        z = next(x for x in c.get("/api/v1/zones").json() if x["id"] == zone)
        assert z["building_id"] is None and len(z["room_ids"]) == 2


def test_google_import_creates_buildings_and_rooms_once():
    with TestClient(app) as c:
        campus = c.post("/api/v1/campuses", json={"name": "Imported"}).json()["id"]
    res = [Resource("gi-a@resource", "Chapel", "bld-1", 120),
           Resource("gi-b@resource", "Room 2", "bld-1", 20),
           Resource("gi-c@resource", "Gym", "bld-2", 300),
           Resource("gi-d@resource", "Annex", None, 10)]
    names = {"bld-1": "Main Building", "bld-2": "Gym Building"}
    p = Principal("admin@example.org", "admin")
    body = api.GoogleImportIn(campuses={"bld-1": campus, "": campus})  # skip bld-2
    with session_scope() as s:
        assert api.import_google_rooms(s, p, body, res, names) == {"buildings": 1, "rooms": 3}
    with session_scope() as s:
        assert api.import_google_rooms(s, p, body, res, names) == {"buildings": 0, "rooms": 0}
    with TestClient(app) as c:
        rooms = {r["name"]: r for r in c.get("/api/v1/rooms").json() if r["campus_id"] == campus}
        assert set(rooms) == {"Chapel", "Room 2", "Annex"}
        b = next(b for b in c.get("/api/v1/buildings").json() if b["campus_id"] == campus)
        assert b["name"] == "Main Building" and rooms["Annex"]["building_id"] is None
        assert rooms["Chapel"]["calendar_id"] == "gi-a@resource"


def test_setup_and_operator_pages_render():
    with TestClient(app) as c:
        campus, bld, rooms, zone = _setup(c, "Rooms5")
        g = c.post("/api/v1/groups", json={"name": "Rooms5 kids", "zone_ids": [zone]}).json()
        for path in ["/", f"/buildings/{bld}", "/setup", "/rooms/new", f"/rooms/{rooms[0]}/edit",
                     f"/zones/{zone}", f"/zones/{zone}/edit", "/zones/new?campus=1",
                     f"/groups/{g['id']}", f"/groups/{g['id']}/edit", "/insights"]:
            assert c.get(path).status_code == 200, path
        # Schedules are edited on operator pages, not in Setup.
        assert 'action="/schedule-blocks"' in c.get(f"/zones/{zone}").text
        assert 'action="/schedule-blocks"' not in c.get(f"/zones/{zone}/edit").text
        assert 'action="/schedule-blocks"' in c.get(f"/groups/{g['id']}").text
        # Form posts: building, room, zone with rooms.
        r = c.post("/buildings", data={"name": "Annex", "campus_id": campus},
                   follow_redirects=False)
        assert r.status_code == 303
        r = c.post("/rooms", data={"name": "Annex 1", "campus_id": campus,
                                   "zone_ids": [zone]}, follow_redirects=False)
        assert r.status_code == 303
        assert any(x["name"] == "Annex 1" and x["zone_ids"] == [zone]
                   for x in c.get("/api/v1/rooms").json())
        r = c.post("/overrides", data={"building_id": bld, "mode": "occupied",
                                        "minutes": "60"}, follow_redirects=False)
        assert r.status_code == 303
        assert reconciler.plan()  # still plans with buildings in play
