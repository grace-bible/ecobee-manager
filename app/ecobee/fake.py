"""In-memory backend for local development and tests (ECOBEE_BACKEND=fake)."""

import math
from datetime import UTC, date, datetime, time, timedelta

from app.ecobee.backend import RuntimeRow, SummaryRow, ThermostatState

_SEED = [
    ("511800000001", "Worship Center North"), ("511800000002", "Worship Center South"),
    ("511800000003", "Kids Wing 101"), ("511800000004", "Kids Wing 102"),
    ("511800000005", "Office Suite"), ("511800000006", "Fellowship Hall"),
]


class FakeBackend:
    def __init__(self):
        self.state = {
            i: ThermostatState(i, n, "nikeSmart", True, 71.5 + k * 0.5, 45, 62, 80, "auto",
                               "", None, "America/Chicago")
            for k, (i, n) in enumerate(_SEED)
        }
        self.rev = 0
        self.has_tokens = True

    def login(self, mfa_code=None):
        pass

    def summary(self):
        return [SummaryRow(s.identifier, s.name, s.connected, f"{self.rev}", s.equipment_running)
                for s in self.state.values()]

    def fetch(self, identifiers):
        return [self.state[i] for i in identifiers if i in self.state]

    def set_hold(self, identifiers, heat, cool, until: datetime, tz):
        self.rev += 1
        for i in identifiers:
            s = self.state[i]
            s.heat_setpoint, s.cool_setpoint = heat, cool
            s.active_hold = {"type": "hold", "running": True, "endDate": until.isoformat()}

    def resume(self, identifiers):
        self.rev += 1
        for i in identifiers:
            s = self.state[i]
            s.heat_setpoint, s.cool_setpoint, s.active_hold = 62, 80, None

    def runtime_report(self, identifiers, start: date, end: date, timezones):
        """Plausible 5-minute history up to now: a daily outdoor swing, a room that drifts
        toward its band, and heating/cooling when it's outside the band."""
        rows = []
        t = datetime.combine(start, time(), UTC)
        stop = min(datetime.combine(end + timedelta(days=1), time(), UTC), datetime.now(UTC))
        for k, i in enumerate(identifiers):
            s = self.state.get(i)
            if not s:
                continue
            temp, at = 70.0 + k, t
            while at < stop:
                hour = at.hour + at.minute / 60
                outdoor = 55 + 15 * math.sin((hour - 15) / 24 * 2 * math.pi)
                busy = 13 <= hour < 23  # ~ 8am–6pm Central
                heat, cool = (68.0, 72.0) if busy else (62.0, 80.0)
                temp += (outdoor - temp) * 0.004
                heat_s = 300 if temp < heat - 0.5 else 0
                cool_s = 300 if temp > cool + 0.5 else 0
                temp += 0.25 * (heat_s - cool_s) / 300
                rows.append(RuntimeRow(i, at, round(temp, 1), 40, heat, cool, round(outdoor, 1),
                                       heat_s, cool_s, max(heat_s, cool_s),
                                       busy and at.minute < 40))
                at += timedelta(minutes=5)
        return rows

