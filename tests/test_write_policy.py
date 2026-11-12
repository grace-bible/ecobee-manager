"""Reconciler re-send policy and poller fetch retries, against the fake backend."""

from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import update

from app import poller, reconciler
from app.config import get_settings
from app.db import session_scope
from app.ecobee.backend import BackendError
from app.main import app
from app.models import Thermostat, aware, utcnow
from app.services import get_backend


def test_no_resend_while_hold_already_ends_at_next_change(monkeypatch):
    """A hold that already ends at the next transition must not be re-sent every minute
    in the last REFRESH_BEFORE window; only the transition itself causes a write."""
    with TestClient(app) as c:
        poller.poll_once()
        therms = {t["name"]: t for t in c.get("/api/v1/thermostats").json()}
        north = therms["Worship Center North"]
        campus = c.post("/api/v1/campuses", json={"name": "Refresh"}).json()["id"]
        zone = c.post("/api/v1/zones", json={
            "name": "Worship North", "campus_id": campus,
            "thermostat_ids": [north["id"]]}).json()["id"]
        c.post("/api/v1/overrides", json={"target": "zone", "target_id": zone, "mode": "custom",
                                          "heat": 69, "cool": 75, "minutes": 10})

        t0 = utcnow()
        monkeypatch.setattr(reconciler, "utcnow", lambda: t0)
        assert reconciler.reconcile_once()["sent"] >= 1

        def row(now):
            return next(p for p in reconciler.plan(now) if p["thermostat_id"] == north["id"])

        for m in range(1, 9):  # hold ends with the override; nothing new to say
            assert not row(t0 + timedelta(minutes=m))["needs_write"], m
        after = row(t0 + timedelta(minutes=11))  # override over → new target
        assert after["needs_write"] and after["source"] != "override"


def test_failed_fetch_is_retried_on_next_poll(monkeypatch):
    """If the full fetch fails, the new revision must not be recorded as seen."""
    poller.poll_once()
    backend = get_backend()
    backend.rev += 1  # every fake thermostat reports a new revision
    with session_scope() as s:
        s.execute(update(Thermostat).values(last_full_fetch=utcnow() - timedelta(minutes=10)))

    def boom(ids):
        raise BackendError("ecobee hiccup")

    monkeypatch.setattr(backend, "fetch", boom)
    assert "error" in poller.poll_once()
    monkeypatch.undo()
    assert poller.poll_once()["fetched"] == 6


def test_zone_status_explains_target_and_override():
    """The operator view: what the room is doing, why, until when, and what comes next."""
    with TestClient(app) as c:
        poller.poll_once()
        therms = {t["name"]: t for t in c.get("/api/v1/thermostats").json()}
        south = therms["Worship Center South"]
        campus = c.post("/api/v1/campuses", json={"name": "Status"}).json()["id"]
        zone = c.post("/api/v1/zones", json={
            "name": "Worship South", "campus_id": campus,
            "thermostat_ids": [south["id"]]}).json()["id"]

        s = c.get(f"/api/v1/zones/{zone}/status").json()
        assert (s["intent"], s["source"]) == ("unoccupied", "default")
        assert (s["heat"], s["cool"]) == (60, 82) and s["override"] is None
        assert s["thermostats"] == 1 and s["running"] in ("heating", "cooling", "fan", "idle")

        o = c.post("/api/v1/overrides", json={"target": "zone", "target_id": zone,
                                              "mode": "occupied", "minutes": 30,
                                              "reason": "choir"}).json()["id"]
        s = c.get(f"/api/v1/zones/{zone}/status").json()
        assert (s["intent"], s["source"]) == ("occupied", "override")
        assert s["override"]["id"] == o and s["override"]["reason"] == "choir"
        assert s["next"]["intent"] == "unoccupied" and s["until"]
        assert f"/overrides/{o}/cancel" in c.get(f"/zones/{zone}").text  # "Back to schedule"
        assert c.get("/api/v1/zones/99999/status").status_code == 404


def test_hold_overlaps_next_change_so_there_is_no_gap(monkeypatch):
    """The reconciler runs once a minute, so a hold that ends exactly at the change would
    leave the thermostat on its own program until the next run. Holds end a little after
    the change (the next hold replaces them), but never beyond the horizon."""
    with TestClient(app) as c:
        poller.poll_once()
        therms = {t["name"]: t for t in c.get("/api/v1/thermostats").json()}
        lobby = therms["Worship Center North"]  # reassigned from any earlier test zone
        campus = c.post("/api/v1/campuses", json={"name": "Overlap"}).json()["id"]
        zone = c.post("/api/v1/zones", json={
            "name": "Lobby zone", "campus_id": campus,
            "thermostat_ids": [lobby["id"]]}).json()["id"]
        c.post("/api/v1/overrides", json={"target": "zone", "target_id": zone, "mode": "off",
                                          "minutes": 20})
        t0 = utcnow()
        monkeypatch.setattr(reconciler, "utcnow", lambda: t0)
        p = next(p for p in reconciler.plan(t0) if p["thermostat_id"] == lobby["id"])
        assert p["hold_until"] - p["until"] == reconciler.HOLD_OVERLAP
        reconciler.reconcile_once()
        with session_scope() as s:
            sent = s.get(Thermostat, lobby["id"]).app_hold_until
        assert aware(sent) == p["hold_until"].replace(second=0, microsecond=0)

        # No change within the horizon: the hold stops at the horizon, no overlap past it.
        horizon = timedelta(minutes=get_settings().hold_horizon_minutes)
        later = t0 + timedelta(minutes=30)  # override over; default all day
        q = next(p for p in reconciler.plan(later) if p["thermostat_id"] == lobby["id"])
        assert q["hold_until"] <= later + horizon
