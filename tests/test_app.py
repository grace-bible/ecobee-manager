"""End-to-end through the API with the fake backend: poll → zone → override → reconcile."""

from fastapi.testclient import TestClient

from app import poller, reconciler
from app.main import app
from app.services import get_backend


def test_flow():
    with TestClient(app) as c:
        assert poller.poll_once()["summary"] == 6
        therms = c.get("/api/v1/thermostats").json()
        ids = [t["id"] for t in therms if "Kids Wing" in t["name"]]

        campus = c.post("/api/v1/campuses", json={"name": "Midtown"}).json()["id"]
        zone = c.post("/api/v1/zones", json={
            "name": "Kids Wing", "campus_id": campus, "thermostat_ids": ids,
            "calendar_id": "c_123@resource.calendar.google.com"}).json()["id"]

        # Nothing booked → default unoccupied setpoints get sent once
        r = reconciler.reconcile_once()
        assert r["sent"] == 2 and r["batches"] == 1
        assert reconciler.reconcile_once()["sent"] == 0  # idempotent

        # Override the zone → new target sent to both thermostats in one batch
        o = c.post("/api/v1/overrides", json={"target": "zone", "target_id": zone,
                                              "mode": "custom", "heat": 70, "cool": 74,
                                              "minutes": 60}).json()["id"]
        assert reconciler.reconcile_once()["sent"] == 2
        state = get_backend().state
        kid_ids = [t["identifier"] for t in therms if t["id"] in ids]
        assert all((state[i].heat_setpoint, state[i].cool_setpoint) == (70, 74) for i in kid_ids)

        # Cancel → back to unoccupied
        c.delete(f"/api/v1/overrides/{o}")
        assert reconciler.reconcile_once()["sent"] == 2
        assert state[kid_ids[0]].heat_setpoint == 60

        # Pages render
        for path in ["/", f"/zones/{zone}", "/zones/new", "/overrides", "/audit", "/settings"]:
            assert c.get(path).status_code == 200, path
        audit = c.get("/api/v1/audit").json()
        assert any(a["action"] == "set_hold" for a in audit)


def test_guardrail_validation():
    with TestClient(app) as c:
        r = c.post("/api/v1/zones", json={"name": "Bad", "campus_id": 1,
                                          "min_setpoint": 80, "max_setpoint": 70})
        assert r.status_code == 422


def test_no_auth_rejected(monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "dev_auth_email", "")
    with TestClient(app) as c:
        assert c.get("/api/v1/thermostats").status_code == 401


def test_schedule_blocks_groups_and_preview():
    with TestClient(app) as c:
        poller.poll_once()
        therms = {t["name"]: t["id"] for t in c.get("/api/v1/thermostats").json()}
        campus = c.post("/api/v1/campuses", json={"name": "Creekside"}).json()["id"]
        zone = c.post("/api/v1/zones", json={
            "name": "Fellowship Hall", "campus_id": campus,
            "thermostat_ids": [therms["Fellowship Hall"]]}).json()["id"]

        bad = c.post("/api/v1/schedule-blocks", json={"zone_id": zone, "weekdays": 1,
                                                      "start": "25:00", "end": "10:00"})
        assert bad.status_code == 422
        # Every day, all day-ish: the zone resolves to its schedule, not the default.
        b = c.post("/api/v1/schedule-blocks", json={
            "zone_id": zone, "weekdays": 127, "start": "00:00", "end": "23:59",
            "mode": "occupied", "label": "Always"}).json()["id"]
        assert [x["id"] for x in c.get(f"/api/v1/schedule-blocks?zone_id={zone}").json()] == [b]
        row = next(p for p in c.get("/api/v1/plan").json() if p["zone"] == "Fellowship Hall")
        assert row["source"] == "schedule" and (row["heat"], row["cool"]) == (68, 72)

        g = c.post("/api/v1/groups", json={"name": "Creekside all", "zone_ids": [zone]}).json()
        upd = c.put(f"/api/v1/groups/{g['id']}", json={"name": "Creekside", "zone_ids": []})
        assert upd.json()["zone_ids"] == []

        for path in ["/groups", f"/groups/{g['id']}", f"/zones/{zone}"]:
            r = c.get(path)
            assert r.status_code == 200, path
        assert "Always" in c.get(f"/zones/{zone}").text

        # Form posts from the UI
        r = c.post("/schedule-blocks", data={"zone_id": zone, "weekdays": ["1", "2"],
                                             "start": "18:00", "end": "21:00", "mode": "custom",
                                             "heat": "70", "cool": "74"},
                   headers={"referer": f"/zones/{zone}"}, follow_redirects=False)
        assert r.status_code == 303
        r = c.post("/schedule-blocks", data={"zone_id": zone, "start": "18:00", "end": "21:00",
                                             "mode": "occupied"},
                   headers={"accept": "text/html"})
        assert r.status_code == 422 and "pick at least one day" in r.text

        assert c.delete(f"/api/v1/schedule-blocks/{b}").json() == {"ok": True}
        assert c.delete(f"/api/v1/groups/{g['id']}").json() == {"ok": True}
        assert c.delete(f"/api/v1/zones/{zone}").json() == {"ok": True}
        assert c.get("/api/v1/thermostats").json()  # thermostats survive, now unassigned


def test_campus_manager_scoping(monkeypatch):
    from app.config import get_settings

    with TestClient(app) as c:
        poller.poll_once()
        therms = {t["name"]: t["id"] for t in c.get("/api/v1/thermostats").json()}
        mine = c.post("/api/v1/campuses", json={"name": "Southwood"}).json()["id"]
        other = c.post("/api/v1/campuses", json={"name": "Anderson"}).json()["id"]
        z_other = c.post("/api/v1/zones", json={
            "name": "Office", "campus_id": other,
            "thermostat_ids": [therms["Office Suite"]]}).json()["id"]

        # Make the manager exist, then give them Southwood only.
        monkeypatch.setattr(get_settings(), "dev_auth_email", "mgr@example.org")
        assert c.get("/api/v1/me").json()["role"] == "viewer"
        assert c.post("/api/v1/campuses", json={"name": "X"}).status_code == 403
        monkeypatch.setattr(get_settings(), "dev_auth_email", "admin@example.org")
        uid = next(u["id"] for u in c.get("/api/v1/users").json()
                   if u["email"] == "mgr@example.org")
        r = c.put(f"/api/v1/users/{uid}", json={"role": "campus_manager", "campus_ids": [mine]})
        assert r.status_code == 200
        o = c.post("/api/v1/overrides", json={"target": "zone", "target_id": z_other,
                                              "mode": "off", "minutes": 30}).json()["id"]

        monkeypatch.setattr(get_settings(), "dev_auth_email", "mgr@example.org")
        z_mine = c.post("/api/v1/zones", json={"name": "Lobby", "campus_id": mine})
        assert z_mine.status_code == 201
        assert c.post("/api/v1/zones", json={"name": "Nope", "campus_id": other}).status_code == 403
        assert c.post("/api/v1/overrides", json={"target": "zone", "target_id": z_other,
                                                 "mode": "off", "minutes": 30}).status_code == 403
        assert c.delete(f"/api/v1/overrides/{o}").status_code == 403
        assert c.post("/api/v1/schedule-blocks", json={
            "zone_id": z_other, "weekdays": 1, "start": "08:00", "end": "09:00"}).status_code == 403
        assert c.get("/api/v1/users").status_code == 403
        # UI hides what they can't change
        assert "Save zone" not in c.get(f"/zones/{z_other}").text
        assert "Save zone" in c.get(f"/zones/{z_mine.json()['id']}").text
        assert f"/overrides/{o}/cancel" not in c.get("/overrides").text


def test_alerts_endpoint_and_pages():
    with TestClient(app) as c:
        assert c.get("/api/v1/alerts").status_code == 200
        assert c.post("/api/v1/jobs/alerts").status_code == 200
        assert "Alerts" in c.get("/settings").text
