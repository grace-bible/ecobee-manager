"""Rollout safety: live switch, special days, campus overrides, operator role, device checks,
all-day bookings."""

from datetime import date, timedelta

from fastapi.testclient import TestClient

from app import poller, reconciler
from app.main import app
from app.models import utcnow


def _zone(c, campus_name, therm="Office Suite", **campus_kw):
    poller.poll_once()
    therms = {t["name"]: t for t in c.get("/api/v1/thermostats").json()}
    cid = c.post("/api/v1/campuses", json={"name": campus_name, **campus_kw}).json()["id"]
    zid = c.post("/api/v1/zones", json={"name": f"{campus_name} zone", "campus_id": cid,
                                        "thermostat_ids": [therms[therm]["id"]]}).json()["id"]
    return cid, zid


def test_zone_can_go_live_ahead_of_its_campus_and_campus_toggle_is_audited():
    with TestClient(app) as c:
        cid, zid = _zone(c, "Live switch")
        row = lambda: next(p for p in reconciler.plan() if p["zone"] == "Live switch zone")
        assert row()["live"] is False
        z = next(x for x in c.get("/api/v1/zones").json() if x["id"] == zid)
        z.update(live=True)
        for k in ("id", "calendar_ids"):
            z.pop(k)
        c.put(f"/api/v1/zones/{zid}", json=z)
        assert row()["live"] is True
        r = c.put(f"/api/v1/campuses/{cid}", json={"name": "Live switch", "live": True})
        assert r.json()["live"] is True
        assert any(a["action"] == "campus_live" for a in c.get("/api/v1/audit").json())


def test_special_days_api_and_effect():
    with TestClient(app) as c:
        cid, zid = _zone(c, "Holidays", therm="Kids Wing 102")
        c.post("/api/v1/schedule-blocks", json={"zone_id": zid, "weekdays": 127, "start": "00:00",
                                                "end": "23:59", "mode": "occupied"})
        assert c.get(f"/api/v1/zones/{zid}/status").json()["source"] == "schedule"
        today = utcnow().astimezone().date()
        r = c.post("/api/v1/special-days", json={
            "campus_id": cid, "first": str(today - timedelta(days=1)),
            "last": str(today + timedelta(days=1)), "closed": True, "closed_mode": "off",
            "label": "Snow week"})
        assert r.status_code == 201
        s = c.get(f"/api/v1/zones/{zid}/status").json()
        assert (s["source"], s["intent"]) == ("special", "off")
        days = c.get(f"/api/v1/special-days?campus_id={cid}").json()
        assert days[0]["label"] == "Snow week"
        assert c.delete(f"/api/v1/special-days/{days[0]['id']}").json() == {"ok": True}
        bad = c.post("/api/v1/special-days", json={"campus_id": cid, "first": "2026-12-25",
                                                    "closed": False, "start": "10:00",
                                                    "end": "09:00"})
        assert bad.status_code == 422 and "end after" in str(bad.json())
        assert c.post("/api/v1/special-days", json={"campus_id": cid, "first": "2026-12-25",
                                                     "last": "2026-12-20"}).status_code == 422


def test_campus_override_and_operator_role():
    with TestClient(app) as c:
        cid, zid = _zone(c, "Operators", therm="Kids Wing 101")
        o = c.post("/api/v1/overrides", json={"target": "campus", "target_id": cid,
                                              "mode": "off", "minutes": 60})
        assert o.status_code == 201
        s = c.get(f"/api/v1/zones/{zid}/status").json()
        assert (s["override"]["scope"], s["override"]["group"]) == ("campus", "Operators")
        c.delete(f"/api/v1/overrides/{o.json()['id']}")

        key = c.post("/api/v1/api-keys", json={"name": "ops-desk", "role": "operator",
                                               "campus_ids": [cid]}).json()["key"]
        h = {"authorization": f"Bearer {key}"}
        r = c.post("/api/v1/overrides", headers=h, json={"target": "zone", "target_id": zid,
                                                         "mode": "occupied", "minutes": 60})
        assert r.status_code == 201
        assert c.delete(f"/api/v1/overrides/{r.json()['id']}", headers=h).status_code == 200
        # …but no setup, schedules or special days.
        assert c.post("/api/v1/schedule-blocks", headers=h, json={
            "zone_id": zid, "weekdays": 1, "start": "08:00", "end": "09:00"}).status_code == 403
        assert c.post("/api/v1/special-days", headers=h, json={
            "campus_id": cid, "first": str(date.today())}).status_code == 403
        assert c.post("/api/v1/rooms", headers=h, json={"name": "x", "campus_id": cid}
                      ).status_code == 403
        assert c.post("/api/v1/api-keys", json={"name": "op2", "role": "operator"}
                      ).status_code == 422  # needs campuses


def test_device_checks():
    from app.checks import mode_issue, program_issue
    assert mode_issue("Gym", "auto") is None
    assert "cool only" in mode_issue("Gym", "cool").message
    assert "turned off" in mode_issue("Gym", "off").message
    safe = {"climates": [{"name": "Away", "heat": 58, "cool": 85}]}
    comfy = {"climates": [{"name": "Home", "heat": 68, "cool": 72},
                          {"name": "Away", "heat": 60, "cool": 82}]}
    assert program_issue("Gym", safe, 60, 82) is None
    i = program_issue("Gym", comfy, 60, 82)
    assert "Home 68–72°" in i.message and "Away" not in i.message


def test_all_day_google_events_are_flagged_in_campus_time():
    from app import gcal
    from app.db import session_scope
    from app.models import CalendarEvent
    ev = {"id": "allday-1", "summary": "Closed for painting", "status": "confirmed",
          "start": {"date": "2026-12-01"}, "end": {"date": "2026-12-02"}}
    with session_scope() as s:
        gcal._upsert(s, "paint@resource", ev, "America/Denver")
    with session_scope() as s:
        row = s.query(CalendarEvent).filter_by(event_id="allday-1").one()
        assert row.all_day is True
        assert row.starts_at.replace(tzinfo=None).hour == 7  # midnight Mountain = 07:00 UTC


def test_pages_forms_and_operator_view(monkeypatch):
    from app.config import get_settings
    with TestClient(app) as c:
        cid, zid = _zone(c, "Pages campus", therm="Worship Center South")
        page = c.get(f"/campuses/{cid}").text
        assert "Closures &amp; special hours" in page and "Dry run" in page
        r = c.post("/special-days", data={"campus_id": cid, "first": "2026-12-25",
                                          "kind": "closed", "closed_mode": "off",
                                          "label": "Christmas Day"},
                   headers={"referer": f"/campuses/{cid}"}, follow_redirects=False)
        assert r.status_code == 303
        assert "Christmas Day" in c.get(f"/campuses/{cid}").text
        r = c.post("/special-days", data={"campus_id": cid, "first": "2026-12-24",
                                          "kind": "hours", "start": "", "end": ""},
                   headers={"accept": "application/json"})
        assert r.status_code == 422 and "start and end" in r.json()["detail"]
        # Go live from Setup
        r = c.post(f"/campuses/{cid}/live", data={"live": "on"}, follow_redirects=False)
        assert r.status_code == 303
        assert next(x for x in c.get("/api/v1/campuses").json() if x["id"] == cid)["live"]
        assert "Back to dry run" in c.get(f"/setup/campuses/{cid}").text
        assert "Health checks" in c.get("/setup").text
        # Operator: quick actions yes, schedule editor and settings no.
        monkeypatch.setattr(get_settings(), "dev_auth_email", "op@example.org")
        c.get("/")  # creates the user as a viewer
        monkeypatch.setattr(get_settings(), "dev_auth_email", "admin@example.org")
        uid = next(u["id"] for u in c.get("/api/v1/users").json() if u["email"] == "op@example.org")
        c.put(f"/api/v1/users/{uid}", json={"role": "operator", "campus_ids": [cid]})
        monkeypatch.setattr(get_settings(), "dev_auth_email", "op@example.org")
        zp = c.get(f"/zones/{zid}").text
        assert "In use · 2h" in zp and 'action="/schedule-blocks"' not in zp
        assert "Zone settings" not in zp
