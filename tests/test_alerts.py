"""Alert thresholds (pure) and the open/resolve/notify cycle."""

from datetime import UTC, datetime, timedelta

from app.alerts import SyncIn, ThermoIn, find_incidents

NOW = datetime(2026, 11, 11, 18, 0, tzinfo=UTC)
M = timedelta(minutes=1)


def therm(**kw):
    base = {"identifier": "1", "name": "Office", "connected": True, "last_seen": NOW}
    return ThermoIn(**(base | kw))


def test_offline_only_after_threshold():
    assert find_incidents(NOW, [therm(connected=False, last_seen=NOW - 14 * M)], None, []) == {}
    got = find_incidents(NOW, [therm(connected=False, last_seen=NOW - 16 * M)], None, [])
    assert got["offline:1"][0] == "offline" and "16 min" in got["offline:1"][1]


def test_never_seen_is_not_offline():
    assert find_incidents(NOW, [therm(connected=False, last_seen=None)], None, []) == {}


def test_setpoint_needs_an_hour_of_occupancy_and_a_real_miss():
    cold = {"current_temp": 63.0, "heat": 68.0, "cool": 72.0}
    assert find_incidents(NOW, [therm(**cold, occupied_for=59 * M)], None, []) == {}
    assert find_incidents(NOW, [therm(**cold, occupied_for=None)], None, []) == {}
    assert "setpoint:1" in find_incidents(NOW, [therm(**cold, occupied_for=60 * M)], None, [])
    close = {"current_temp": 66.0, "heat": 68.0, "cool": 72.0}  # within 3°F tolerance
    assert find_incidents(NOW, [therm(**close, occupied_for=90 * M)], None, []) == {}
    hot = {"current_temp": 76.0, "heat": 68.0, "cool": 72.0}
    assert "cooling" in find_incidents(NOW, [therm(**hot, occupied_for=90 * M)], None, [])[
        "setpoint:1"][1]


def test_ecobee_auth_and_calendar():
    got = find_incidents(NOW, [], "MFA code rejected", [
        SyncIn("room-a", NOW - 5 * M, "403"),     # failing, but recently fine → wait
        SyncIn("room-b", NOW - 20 * M, "403"),    # failing for 20 min → alert
        SyncIn("room-c", None, "403"),            # never synced → alert
        SyncIn("room-d", NOW - 60 * M, None),     # no error → fine
    ])
    assert set(got) == {"ecobee_auth", "calendar:room-b", "calendar:room-c"}


def test_check_once_opens_once_and_resolves(monkeypatch):
    from app import alerts

    sent = []
    monkeypatch.setattr(alerts, "send_chat", lambda text, url=None: sent.append(text) or True)
    incidents = {"offline:9": ("offline", "Hall has been offline for 20 min")}
    monkeypatch.setattr(alerts, "gather_with_campuses", lambda now: (dict(incidents), {}))

    assert alerts.check_once(NOW)["opened"] == 1
    assert alerts.check_once(NOW + M)["opened"] == 0  # debounced: still open, no new message
    assert len(sent) == 1 and sent[0].startswith("⚠️")
    incidents.clear()
    assert alerts.check_once(NOW + 2 * M)["resolved"] == 1
    assert sent[-1].startswith("✅") and len(sent) == 2
    assert alerts.open_alerts() == []


def test_ecobee_device_alerts_become_incidents():
    a = {"ref": "abc-1", "text": "Heating is not working", "severity": "high", "type": "alert"}
    got = find_incidents(NOW, [therm(ecobee_alerts=(a,))], None, [])
    assert got["ecobee:1:abc-1"] == ("ecobee", "Office: Heating is not working")
    assert find_incidents(NOW, [therm(ecobee_alerts=())], None, []) == {}


def test_managed_thermostat_not_in_auto_is_an_incident():
    got = find_incidents(NOW, [therm(hvac_mode="cool")], None, [])
    assert got["mode:1"][0] == "mode" and "cool only" in got["mode:1"][1]
    assert find_incidents(NOW, [therm(hvac_mode="auto")], None, []) == {}
    assert find_incidents(NOW, [therm(hvac_mode=None)], None, []) == {}  # unmanaged


def test_alerts_route_to_the_campus_webhook(monkeypatch):
    from fastapi.testclient import TestClient

    from app import alerts
    from app.main import app

    hook = "https://chat.googleapis.com/v1/spaces/X/messages?key=k"
    with TestClient(app) as c:
        cid = c.post("/api/v1/campuses", json={"name": "Routed"}).json()["id"]
        assert c.put(f"/api/v1/campuses/{cid}", json={"name": "Routed", "chat_webhook": "nope"}
                     ).status_code == 422
        c.put(f"/api/v1/campuses/{cid}", json={"name": "Routed", "chat_webhook": hook})
        assert next(x for x in c.get("/api/v1/campuses").json() if x["id"] == cid)[
            "has_chat_webhook"]
    sent = []
    monkeypatch.setattr(alerts, "send_chat", lambda text, url=None: sent.append(url) or True)
    monkeypatch.setattr(alerts, "gather_with_campuses", lambda now: (
        {"offline:r1": ("offline", "A is offline"), "ecobee_auth": ("ecobee_auth", "login")},
        {"offline:r1": cid, "ecobee_auth": None}))
    alerts.check_once(NOW + 10 * M)
    assert sorted(sent, key=str) == sorted([hook, None], key=str)
    assert alerts.campus_for_key("ecobee:abc:ref", {"abc": 7}, {}) == 7
    assert alerts.campus_for_key("calendar:c@r", {}, {"c@r": 3}) == 3
