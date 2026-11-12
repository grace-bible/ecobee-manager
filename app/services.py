"""Process-wide singletons: the thermostat backend and encrypted token storage."""

from __future__ import annotations

import logging
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings
from app.db import session_scope
from app.ecobee.backend import EcobeeCloudBackend
from app.ecobee.dryrun import DryRunBackend
from app.ecobee.fake import FakeBackend
from app.models import EcobeeCredentials, utcnow

log = logging.getLogger(__name__)


def _fernet() -> Fernet:
    key = get_settings().app_secret_key
    if not key:
        raise RuntimeError("APP_SECRET_KEY is not set (generate one with: "
                           "python -c 'from cryptography.fernet import Fernet; "
                           "print(Fernet.generate_key().decode())')")
    return Fernet(key.encode())


def _enc(v: str | None) -> str | None:
    return _fernet().encrypt(v.encode()).decode() if v else None


def _dec(v: str | None) -> str | None:
    if not v:
        return None
    try:
        return _fernet().decrypt(v.encode()).decode()
    except InvalidToken:
        log.warning("Stored ecobee token can't be decrypted (APP_SECRET_KEY changed?); ignoring")
        return None


def load_tokens() -> tuple[str | None, str | None]:
    with session_scope() as s:
        row = s.get(EcobeeCredentials, 1)
        return (_dec(row.refresh_token_enc), _dec(row.access_token_enc)) if row else (None, None)


def save_tokens(refresh: str | None, access: str | None) -> None:
    with session_scope() as s:
        row = s.get(EcobeeCredentials, 1) or EcobeeCredentials(id=1)
        row.refresh_token_enc, row.access_token_enc = _enc(refresh), _enc(access)
        row.last_login, row.last_error = utcnow(), None
        s.add(row)


def record_auth_error(msg: str) -> None:
    with session_scope() as s:
        row = s.get(EcobeeCredentials, 1) or EcobeeCredentials(id=1)
        row.last_error = msg
        s.add(row)


@lru_cache
def raw_backend():
    """The real backend (fake in dev/tests). Writes go through it only for live zones."""
    st = get_settings()
    if st.ecobee_backend == "fake":
        return FakeBackend()
    return EcobeeCloudBackend(st.ecobee_username, st.ecobee_password, st.ecobee_totp_secret,
                              load_tokens, save_tokens)


@lru_cache
def dry_backend():
    return DryRunBackend(raw_backend())


def get_backend():
    """Reads (and writes when DRY_RUN is on, the master switch, everything is dry)."""
    return dry_backend() if get_settings().dry_run else raw_backend()


def zone_is_live(z) -> bool:
    """Holds are really sent only when DRY_RUN is off and the zone (or its campus) is live."""
    if get_settings().dry_run:
        return False
    return bool(z.live if z.live is not None else (z.campus.live if z.campus else False))
