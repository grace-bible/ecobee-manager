"""Managing campuses, thermostat assignment and the Setup screens."""

from fastapi.testclient import TestClient

from app import poller
from app.main import app


def test_edit_and_delete_campus():
    with TestClient(app) as c:
        cid = c.post("/api/v1/campuses", json={"name": "Edit me"}).json()["id"]
        r = c.put(f"/api/v1/campuses/{cid}", json={"name": "Edited", "timezone": "America/Denver"})
        assert r.status_code == 200 and r.json()["timezone"] == "America/Denver"
        assert c.put(f"/api/v1/campuses/{cid}", json={"name": "Edited",
                                                      "timezone": "Mars/Olympus"}).status_code == 422
        other = c.post("/api/v1/campuses", json={"name": "Taken"}).json()["id"]
        assert c.put(f"/api/v1/campuses/{cid}", json={"name": "Taken"}).status_code == 409

        # Not empty → refused; empty → gone.
        c.post("/api/v1/buildings", json={"name": "B", "campus_id": cid})
        assert c.delete(f"/api/v1/campuses/{cid}").status_code == 409
        assert c.delete(f"/api/v1/campuses/{other}").json() == {"ok": True}
        assert all(x["id"] != other for x in c.get("/api/v1/campuses").json())


def test_assign_thermostat_to_zone():
    with TestClient(app) as c:
        poller.poll_once()
        therms = {t["name"]: t for t in c.get("/api/v1/thermostats").json()}
        tid = therms["Office Suite"]["id"]
        cid = c.post("/api/v1/campuses", json={"name": "Assign"}).json()["id"]
        zid = c.post("/api/v1/zones", json={"name": "Offices", "campus_id": cid}).json()["id"]
        r = c.put(f"/api/v1/thermostats/{tid}/zone", json={"zone_id": zid})
        assert r.status_code == 200 and r.json()["zone_id"] == zid
        assert tid in next(z for z in c.get("/api/v1/zones").json() if z["id"] == zid)[
            "thermostat_ids"]
        assert c.put(f"/api/v1/thermostats/{tid}/zone", json={"zone_id": None}
                     ).json()["zone_id"] is None
        # Form version, from the Setup list of unassigned thermostats
        r = c.post(f"/thermostats/{tid}/zone", data={"zone_id": zid}, follow_redirects=False)
        assert r.status_code == 303


def test_setup_pages_show_the_tree_and_details():
    with TestClient(app) as c:
        cid = c.post("/api/v1/campuses", json={"name": "Tree campus"}).json()["id"]
        bid = c.post("/api/v1/buildings", json={"name": "Tree bldg", "campus_id": cid}).json()["id"]
        rid = c.post("/api/v1/rooms", json={"name": "Tree room", "campus_id": cid,
                                            "building_id": bid}).json()["id"]
        zid = c.post("/api/v1/zones", json={"name": "Tree zone", "campus_id": cid,
                                            "building_id": bid, "room_ids": [rid]}).json()["id"]
        for path in ["/setup", f"/setup/campuses/{cid}", f"/setup/buildings/{bid}",
                     f"/zones/{zid}/edit", f"/rooms/{rid}/edit", "/zones/new", "/rooms/new"]:
            page = c.get(path)
            assert page.status_code == 200, path
            assert 'class="tree"' in page.text and "Tree zone" in page.text, path
        assert 'action="/campuses/' in c.get(f"/setup/campuses/{cid}").text
        r = c.post(f"/campuses/{cid}", data={"name": "Tree campus 2",
                                             "timezone": "America/Chicago"},
                   follow_redirects=False)
        assert r.status_code == 303
        assert "Tree campus 2" in c.get("/setup").text
