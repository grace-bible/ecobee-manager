"""Wraps a backend so reads are real but writes are only logged.

DRY_RUN=true is the default so a fresh deploy can't touch a thermostat until someone has
looked at what the reconciler *would* do in the audit log."""

import logging
from datetime import datetime

log = logging.getLogger(__name__)


class DryRunBackend:
    def __init__(self, inner):
        self._inner = inner
        self.writes: list[dict] = []

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def set_hold(self, identifiers, heat, cool, until: datetime, tz):
        self.writes.append({"op": "set_hold", "ids": list(identifiers), "heat": heat,
                            "cool": cool, "until": until.isoformat()})
        log.info("DRY RUN set_hold %s heat=%s cool=%s until=%s", identifiers, heat, cool, until)

    def resume(self, identifiers):
        self.writes.append({"op": "resume", "ids": list(identifiers)})
        log.info("DRY RUN resume %s", identifiers)
