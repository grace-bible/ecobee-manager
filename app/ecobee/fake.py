"""In-memory backend for local development and tests (ECOBEE_BACKEND=fake)."""

from datetime import datetime

from app.ecobee.backend import SummaryRow, ThermostatState

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
