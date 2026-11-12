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
        history.sync_once()  # re-reads the last couple of hours; no duplicates
        with session_scope() as s:
            # (The fake runs up to "now", so a new 5-minute interval may legitimately appear
            # between the two syncs; compare rows with distinct intervals, not totals.)
            total = s.scalar(select(func.count()).select_from(RuntimeSample))
            distinct = s.scalar(select(func.count()).select_from(
                select(RuntimeSample.thermostat_id, RuntimeSample.at).distinct().subquery()))
            assert total == distinct

        therms = {t["name"]: t for t in c.get("/api/v1/thermostats").json()}
        campus = c.post("/api/v1/campuses", json={"name": "History"}).json()["id"]
        zone = c.post("/api/v1/zones", json={
            "name": "Hall", "campus_id": campus,
            "thermostat_ids": [therms["Fellowship Hall"]["id"]]}).json()["id"]

        pts = c.get(f"/api/v1/zones/{zone}/history?hours=24").json()
        assert len(pts) > 200 and {"temp", "heat_sp", "heat_s"} <= set(pts[0])
        ins = c.get(f"/api/v1/zones/{zone}/insights").json()
        assert ins["samples"] > 0 and ins["runtime"] and ins["occupancy"]["has_data"]
        assert ins["time_to_target_summary"]["count"] > 0  # the fake has a daily schedule

        page = c.get(f"/zones/{zone}").text
        assert 'class="chart"' in page and "Time to target" in page
        for rng in ("3d", "7d", "30d"):
            assert 'class="chart"' in c.get(f"/zones/{zone}?range={rng}").text, rng
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


def test_cards_follow_the_chart_range():
    with TestClient(app) as c:
        poller.poll_once()
        history.sync_once()
        therms = {t["name"]: t for t in c.get("/api/v1/thermostats").json()}
        campus = c.post("/api/v1/campuses", json={"name": "Ranges"}).json()["id"]
        zone = c.post("/api/v1/zones", json={
            "name": "Office", "campus_id": campus,
            "thermostat_ids": [therms["Office Suite"]["id"]]}).json()["id"]
        day = c.get(f"/api/v1/zones/{zone}/insights?days=1").json()
        month = c.get(f"/api/v1/zones/{zone}/insights?days=30").json()
        assert day["runtime_step"] == "hour" and 20 <= len(day["runtime"]) <= 25
        assert month["runtime_step"] == "day" and len(month["runtime"]) >= 28
        assert month["samples"] > day["samples"]
        page = c.get(f"/zones/{zone}?range=30d").text
        assert "last 30 days" in page and "last 7 days" not in page
        assert "last 24 hours" in c.get(f"/zones/{zone}").text
