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
from dataclasses import dataclass
from datetime import datetime
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
    revision: str  # thermostat + runtime revisions; change ⇒ worth a full fetch
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


class ThermostatBackend(Protocol):
    def summary(self) -> list[SummaryRow]: ...
    def fetch(self, identifiers: list[str]) -> list[ThermostatState]: ...
    def set_hold(self, identifiers: list[str], heat: float, cool: float,
                 until: datetime, tz: str) -> None: ...
    def resume(self, identifiers: list[str]) -> None: ...


def _chunks(items: list[str], n: int = BATCH) -> Iterable[list[str]]:
    for i in range(0, len(items), n):
        yield items[i:i + n]


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
                revision=f"{p[3]}:{p[5]}", equipment=status.get(p[0], ""),
            ))
        return rows

    def fetch(self, identifiers: list[str]) -> list[ThermostatState]:
        out: list[ThermostatState] = []
        for chunk in _chunks(identifiers):
            sel = {"selection": {
                "selectionType": "thermostats", "selectionMatch": ",".join(chunk),
                "includeRuntime": True, "includeSettings": True, "includeEvents": True,
                "includeEquipmentStatus": True, "includeLocation": True,
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
                ))
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
