"""Reconciler re-send policy and poller fetch retries, against the fake backend."""

from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import update

from app import poller, reconciler
from app.db import session_scope
from app.ecobee.backend import BackendError
from app.main import app
from app.models import Thermostat, utcnow
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
