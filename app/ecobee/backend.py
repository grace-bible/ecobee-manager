"""Thermostat backends.

`ThermostatBackend` is the only surface the rest of the app uses. Today there is one
implementation, `EcobeeCloudBackend`, built on python-ecobee-api's web login (the same
unofficial path Home Assistant uses since 2026.3). A HomeKit/local backend can be added
later behind the same interface (docs/SPEC.md §10).

Temperatures cross this boundary in °F; ecobee's API uses integer tenths.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

import pyotp
from pyecobee import Ecobee
from pyecobee.errors import (
    EcobeeAuthFailedError,
    EcobeeAuthMfaRequiredError,
    EcobeeAuthUnknownError,
    EcobeeError,
    InvalidTokenError,
)

log = logging.getLogger(__name__)

BATCH = 25  # ecobee allows at most 25 thermostats per selection


class BackendError(Exception):
    pass


class AuthRequired(BackendError):
    """Unattended login isn't possible (bad password, MFA without a TOTP secret, …)."""


@dataclass
class SummaryRow:
    identifier: str
    name: str
    connected: bool
    revision: str  # thermostat + alerts + runtime revisions; change ⇒ worth a full fetch
    equipment: str


@dataclass
class ThermostatState:
    identifier: str
    name: str
    model: str | None
    connected: bool
    current_temp: float | None
    humidity: int | None
    heat_setpoint: float | None
    cool_setpoint: float | None
    hvac_mode: str | None
    equipment_running: str
    active_hold: dict | None
    timezone: str | None
    # Active ecobee alerts on the device: [{"ref", "text", "severity", "type"}]
    alerts: list[dict] = field(default_factory=list)
    # The device's own program (its fallback if the app stops): the comfort settings its
    # weekly schedule uses, in °F: {"climates": [{"name", "heat", "cool"}]}
    program: dict | None = None


@dataclass
class RuntimeRow:
    """One 5-minute interval from ecobee's runtime report (history)."""

    identifier: str
    at: datetime  # UTC start of the interval
    temp: float | None
    humidity: int | None
    heat_sp: float | None
    cool_sp: float | None
    outdoor: float | None
    heat_s: int  # seconds of heating in the interval (0–300)
    cool_s: int
    fan_s: int
    occupied: bool | None  # thermostat's own occupancy sensor, if it has one


class ThermostatBackend(Protocol):
    def summary(self) -> list[SummaryRow]: ...
    def fetch(self, identifiers: list[str]) -> list[ThermostatState]: ...
    def set_hold(self, identifiers: list[str], heat: float, cool: float,
                 until: datetime, tz: str) -> None: ...
    def resume(self, identifiers: list[str]) -> None: ...
    def runtime_report(self, identifiers: list[str], start: date, end: date,
                       timezones: dict[str, str]) -> list[RuntimeRow]: ...


def _chunks(items: list[str], n: int = BATCH) -> Iterable[list[str]]:
    for i in range(0, len(items), n):
        yield items[i:i + n]


_RUNTIME_COLUMNS = ("zoneAveTemp,zoneHumidity,zoneHeatTemp,zoneCoolTemp,outdoorTemp,"
                    "compHeat1,compHeat2,auxHeat1,auxHeat2,compCool1,compCool2,fan,zoneOccupancy")


def _runtime_row(ident: str, line: str, tz: ZoneInfo) -> RuntimeRow | None:
    """'2026-11-12,15:05:00,74.4,36,45,90.7,78,0,0,0,0,0,0,0,1' → RuntimeRow (None if empty)."""
    p = line.split(",")
    if len(p) < 15 or p[2] == "":
        return None

    def num(v):
        return float(v) if v not in ("", None) else None

    def secs(*vs):
        return int(max((float(v) for v in vs if v not in ("", None)), default=0))

    at = datetime.fromisoformat(f"{p[0]}T{p[1]}").replace(tzinfo=tz).astimezone(ZoneInfo("UTC"))
    hum = num(p[3])
    return RuntimeRow(ident, at, num(p[2]), int(hum) if hum is not None else None,
                      num(p[4]), num(p[5]), num(p[6]),
                      heat_s=secs(p[7], p[8], p[9], p[10]), cool_s=secs(p[11], p[12]),
                      fan_s=secs(p[13]), occupied=None if p[14] == "" else p[14] == "1")


def _program(prog: dict) -> dict | None:
    """Comfort settings the device's weekly schedule actually uses (°F)."""
    used = {ref for day in prog.get("schedule", []) for ref in day}
    climates = [{"name": c.get("name") or c.get("climateRef"), "heat": _f(c.get("heatTemp")),
                 "cool": _f(c.get("coolTemp"))}
                for c in prog.get("climates", []) if c.get("climateRef") in used]
    return {"climates": climates} if climates else None


def _f(tenths) -> float | None:
    if tenths is None or tenths in (-5002, -5003):  # unknown / calibrating
        return None
    return round(tenths / 10, 1)


class EcobeeCloudBackend:
    """ecobee cloud API using the account's own login.

    `load_tokens` / `save_tokens` persist (refresh_token, access_token) — the app stores
    them encrypted in Postgres so a container restart doesn't force a new login.
    """

    def __init__(
        self,
        username: str,
        password: str,
        totp_secret: str,
        load_tokens: Callable[[], tuple[str | None, str | None]],
        save_tokens: Callable[[str | None, str | None], None],
    ):
        self._username = username
        self._password = password
        self._totp_secret = totp_secret
        self._save_tokens = save_tokens
        refresh, access = load_tokens()
        self._eco = Ecobee(config={"USERNAME": username, "PASSWORD": password,
                                   "REFRESH_TOKEN": refresh, "ACCESS_TOKEN": access})
        self._lock = threading.RLock()  # library object isn't thread-safe

    @property
    def has_tokens(self) -> bool:
        return bool(self._eco.refresh_token)

    # ---------- auth ----------

    def login(self, mfa_code: str | None = None) -> None:
        with self._lock:
            self._login(mfa_code)

    def _login(self, mfa_code: str | None = None) -> None:
        """Full web login. Uses the stored TOTP secret for MFA unless a code is given."""
        try:
            self._eco.request_tokens_web()
        except EcobeeAuthMfaRequiredError as err:
            code = mfa_code or (pyotp.TOTP(self._totp_secret).now() if self._totp_secret else None)
            if not code:
                raise AuthRequired("ecobee wants an MFA code and no TOTP secret is configured") from err
            if err.challenge.mfa_type != "otp" and not mfa_code:
                raise AuthRequired(f"ecobee wants {err.challenge.mfa_type} MFA; enter the code in Settings") from err
            try:
                self._eco.submit_mfa_code(err.challenge, code)
            except EcobeeAuthFailedError as e:
                raise AuthRequired(f"MFA code rejected: {e}") from e
        except EcobeeAuthFailedError as err:
            raise AuthRequired(f"ecobee rejected the login: {err}") from err
        except EcobeeAuthUnknownError as err:
            raise BackendError(f"ecobee login flow changed or failed: {err}") from err
        self._persist()
        log.info("ecobee login succeeded")

    def _persist(self) -> None:
        self._save_tokens(self._eco.refresh_token, self._eco.access_token)

    def _call(self, method: str, endpoint: str, action: str, params=None, body=None) -> dict:
        with self._lock:
            if not self._eco.access_token:
                if self._eco.refresh_token:
                    self._refresh_or_login()
                else:
                    self.login()
            before = (self._eco.refresh_token, self._eco.access_token)
            try:
                # Private helper, but it carries the library's expired-token retry and
                # ecobee status-code mapping. Pinned version; revisit on upgrade.
                resp = self._eco._request_with_refresh(method, endpoint, action, params=params, body=body)
            except InvalidTokenError:
                self._eco.refresh_token = None
                self.login()
                resp = self._eco._request_with_refresh(method, endpoint, action, params=params, body=body)
            except EcobeeError as err:
                raise BackendError(str(err)) from err
            if (self._eco.refresh_token, self._eco.access_token) != before:
                self._persist()
        if resp is None:
            raise BackendError(f"ecobee request failed: {action}")
        status = resp.get("status", {}) if isinstance(resp, dict) else {}
        if status.get("code", 0) != 0:
            raise BackendError(f"ecobee {action}: {status.get('code')} {status.get('message')}")
        return resp

    def _refresh_or_login(self) -> None:
        try:
            self._eco.refresh_tokens()
        except InvalidTokenError:
            self._eco.refresh_token = None
            self.login()
        self._persist()

    # ---------- reads ----------

    def summary(self) -> list[SummaryRow]:
        sel = {"selection": {"selectionType": "registered", "selectionMatch": "",
                             "includeEquipmentStatus": True}}
        resp = self._call("GET", "thermostatSummary", "thermostat summary",
                          params={"json": json.dumps(sel)})
        status = {}
        for row in resp.get("statusList", []):
            ident, _, equip = row.partition(":")
            status[ident] = equip
        rows = []
        # "id:name:connected:thermostatRev:alertsRev:runtimeRev:intervalRev"
        for row in resp.get("revisionList", []):
            p = row.split(":")
            rows.append(SummaryRow(
                identifier=p[0], name=p[1], connected=p[2] == "true",
                revision=f"{p[3]}:{p[4]}:{p[5]}", equipment=status.get(p[0], ""),
            ))
        return rows

    def fetch(self, identifiers: list[str]) -> list[ThermostatState]:
        out: list[ThermostatState] = []
        for chunk in _chunks(identifiers):
            sel = {"selection": {
                "selectionType": "thermostats", "selectionMatch": ",".join(chunk),
                "includeRuntime": True, "includeSettings": True, "includeEvents": True,
                "includeEquipmentStatus": True, "includeLocation": True,
                "includeAlerts": True, "includeProgram": True,
            }}
            resp = self._call("GET", "thermostat", "get thermostats",
                              params={"json": json.dumps(sel)})
            for t in resp.get("thermostatList", []):
                rt, st = t.get("runtime", {}), t.get("settings", {})
                hold = next((e for e in t.get("events", [])
                             if e.get("running") and e.get("type") == "hold"), None)
                out.append(ThermostatState(
                    identifier=t["identifier"], name=t.get("name", t["identifier"]),
                    model=t.get("modelNumber"),
                    connected=rt.get("connected", False),
                    current_temp=_f(rt.get("actualTemperature")),
                    humidity=rt.get("actualHumidity"),
                    heat_setpoint=_f(rt.get("desiredHeat")),
                    cool_setpoint=_f(rt.get("desiredCool")),
                    hvac_mode=st.get("hvacMode"),
                    equipment_running=t.get("equipmentStatus", ""),
                    active_hold=hold,
                    timezone=t.get("location", {}).get("timeZone"),
                    program=_program(t.get("program") or {}),
                    alerts=[{"ref": a.get("acknowledgeRef"), "text": a.get("text", ""),
                             "severity": a.get("severity"), "type": a.get("alertType")}
                            for a in t.get("alerts", [])],
                ))
        return out

    def runtime_report(self, identifiers: list[str], start: date, end: date,
                       timezones: dict[str, str]) -> list[RuntimeRow]:
        """5-minute history. ecobee returns rows in each thermostat's local time; we
        convert to UTC with `timezones`. At most 31 days and 25 thermostats per request."""
        out: list[RuntimeRow] = []
        day = start
        while day <= end:
            last = min(end, day + timedelta(days=30))
            for chunk in _chunks(identifiers):
                req = {"startDate": day.isoformat(), "endDate": last.isoformat(),
                       "columns": _RUNTIME_COLUMNS,
                       "selection": {"selectionType": "thermostats",
                                     "selectionMatch": ",".join(chunk)}}
                resp = self._call("GET", "runtimeReport", "runtime report",
                                  params={"format": "json", "body": json.dumps(req)})
                for rep in resp.get("reportList", []):
                    ident = rep["thermostatIdentifier"]
                    tz = ZoneInfo(timezones.get(ident) or "UTC")
                    out.extend(r for r in (_runtime_row(ident, line, tz)
                                           for line in rep.get("rowList", [])) if r)
            day = last + timedelta(days=1)
        return out

    # ---------- writes ----------

    def set_hold(self, identifiers: list[str], heat: float, cool: float,
                 until: datetime, tz: str) -> None:
        """dateTime hold from now until `until`, in the thermostat's local time.
        Every thermostat in `identifiers` gets the same target in one request per 25."""
        zone = ZoneInfo(tz)
        start = datetime.now(zone)
        end = until.astimezone(zone)
        params = {
            "holdType": "dateTime",
            "startDate": start.strftime("%Y-%m-%d"), "startTime": start.strftime("%H:%M:%S"),
            "endDate": end.strftime("%Y-%m-%d"), "endTime": end.strftime("%H:%M:%S"),
            "heatHoldTemp": int(round(heat * 10)), "coolHoldTemp": int(round(cool * 10)),
        }
        for chunk in _chunks(identifiers):
            body = {"selection": {"selectionType": "thermostats", "selectionMatch": ",".join(chunk)},
                    "functions": [{"type": "setHold", "params": params}]}
            self._call("POST", "thermostat", "set hold", body=body)

    def resume(self, identifiers: list[str]) -> None:
        for chunk in _chunks(identifiers):
            body = {"selection": {"selectionType": "thermostats", "selectionMatch": ",".join(chunk)},
                    "functions": [{"type": "resumeProgram", "params": {"resumeAll": True}}]}
            self._call("POST", "thermostat", "resume program", body=body)
