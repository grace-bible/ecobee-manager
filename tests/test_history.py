"""History sync (fake backend) → samples → chart, insights page and API."""

from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import history, poller
from app.db import session_scope
from app.main import app
from app.models import RuntimeSample, Thermostat, utcnow


def test_sync_stores_once_and_pages_use_it():
    with TestClient(app) as c:
        poller.poll_once()
        with session_scope() as s:
            assert {t.timezone for t in s.scalars(select(Thermostat))} == {"America/Chicago"}

        first = history.sync_once()
        assert first["stored"] > 0 and "error" not in first
        with session_scope() as s:
            n = s.scalar(select(func.count()).select_from(RuntimeSample))
        history.sync_once()  # re-reads the last couple of hours; no duplicates
        with session_scope() as s:
            assert s.scalar(select(func.count()).select_from(RuntimeSample)) == n

        therms = {t["name"]: t for t in c.get("/api/v1/thermostats").json()}
        campus = c.post("/api/v1/campuses", json={"name": "History"}).json()["id"]
        zone = c.post("/api/v1/zones", json={
            "name": "Hall", "campus_id": campus,
            "thermostat_ids": [therms["Fellowship Hall"]["id"]]}).json()["id"]

        pts = c.get(f"/api/v1/zones/{zone}/history?hours=24").json()
        assert len(pts) > 200 and {"temp", "heat_sp", "heat_s"} <= set(pts[0])
        ins = c.get(f"/api/v1/zones/{zone}/insights").json()
        assert ins["samples"] > 0 and ins["runtime"] and ins["occupancy"]["has_data"]
        assert ins["warmup_summary"]["count"] > 0  # the fake has a daily schedule

        page = c.get(f"/zones/{zone}").text
        assert 'class="chart"' in page and "Getting to temperature" in page
        assert 'class="chart"' in c.get(f"/zones/{zone}?range=7d").text
        assert "Hall" in c.get("/insights").text and c.get("/insights?days=30").status_code == 200


def test_old_samples_are_pruned(monkeypatch):
    from app.config import get_settings
    poller.poll_once()
    history.sync_once()
    monkeypatch.setattr(get_settings(), "history_keep_days", 1)
    history.sync_once(utcnow() + timedelta(days=1))
    with session_scope() as s:
        oldest = s.scalar(select(func.min(RuntimeSample.at)))
    assert oldest is None or utcnow() - oldest.replace(tzinfo=utcnow().tzinfo) < timedelta(days=1)
