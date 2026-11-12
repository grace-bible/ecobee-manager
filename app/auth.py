"""Who is calling? (docs/SPEC.md §7)

1. Human via Cloudflare Access → verify Cf-Access-Jwt-Assertion (signature, AUD, issuer),
   take `email`, look up/create app_user for the role.
2. Machine → `Authorization: Bearer emk_…` API key (still behind a Cloudflare Access
   service token at the edge).
3. DEV_AUTH_EMAIL → local development only; refused in production.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass, field
from functools import lru_cache

import jwt
from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import ApiKey, AppUser, utcnow

log = logging.getLogger(__name__)
API_KEY_PREFIX = "emk_"
# Roles, most to least: admin (everything), campus_manager (setup, schedules and changes on
# their campuses), operator (temporary changes on their campuses only), viewer (read-only).
SCOPED_ROLES = ("campus_manager", "operator")


@dataclass
class Principal:
    name: str  # email or api key name
    role: str  # admin / campus_manager / operator / viewer
    campus_ids: list[int] = field(default_factory=list)
    is_machine: bool = False

    @property
    def can_write(self) -> bool:
        """Setup and schedules (and everything an operator can do)."""
        return self.role in ("admin", "campus_manager")

    @property
    def can_override(self) -> bool:
        """Temporary changes: In use / Off / Adjust / Back to schedule."""
        return self.role in ("admin", "campus_manager", "operator")

    def may_touch_campus(self, campus_id: int | None) -> bool:
        return self.role == "admin" or (
            self.role in SCOPED_ROLES and campus_id in (self.campus_ids or []))


@lru_cache
def _jwks_client(team_domain: str) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(f"https://{team_domain}/cdn-cgi/access/certs", cache_keys=True)


def verify_cf_jwt(token: str) -> dict:
    st = get_settings()
    if not (st.cf_access_team_domain and st.cf_access_aud):
        raise HTTPException(500, "Cloudflare Access is not configured")
    key = _jwks_client(st.cf_access_team_domain).get_signing_key_from_jwt(token)
    return jwt.decode(token, key.key, algorithms=["RS256"], audience=st.cf_access_aud,
                      issuer=f"https://{st.cf_access_team_domain}")


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def new_api_key() -> str:
    return API_KEY_PREFIX + secrets.token_urlsafe(32)


def _user_for_email(db: Session, email: str) -> Principal:
    st = get_settings()
    email = email.lower()
    user = db.scalar(select(AppUser).where(AppUser.email == email))
    if user is None:
        domain = email.rsplit("@", 1)[-1]
        if email in st.admin_emails:
            role = "admin"
        elif domain in st.allowed_email_domains:
            role = "viewer"
        else:
            raise HTTPException(403, f"{email} is not allowed")
        user = AppUser(email=email, role=role)
        db.add(user)
    elif email in st.admin_emails and user.role != "admin":
        user.role = "admin"  # ADMIN_EMAILS always wins, so you can't lock yourself out
    user.last_login = utcnow()
    return Principal(user.email, user.role, list(user.campus_ids or []))


def current_principal(request: Request, db: Session = Depends(get_db)) -> Principal:
    st = get_settings()

    authz = request.headers.get("authorization", "")
    if authz.lower().startswith("bearer ") and authz[7:].startswith(API_KEY_PREFIX):
        key = db.scalar(select(ApiKey).where(ApiKey.key_hash == hash_key(authz[7:].strip())))
        if not key:
            raise HTTPException(401, "invalid API key")
        return Principal(f"api:{key.name}", key.role, list(key.campus_ids or []), is_machine=True)

    token = request.headers.get("cf-access-jwt-assertion") or request.cookies.get("CF_Authorization")
    if token:
        try:
            claims = verify_cf_jwt(token)
        except jwt.PyJWTError as err:
            log.warning("rejected Cloudflare Access token: %s", err)
            raise HTTPException(401, "invalid Cloudflare Access token") from err
        email = claims.get("email")
        if not email:
            raise HTTPException(401, "service tokens must also send an API key")
        return _user_for_email(db, email)

    if st.dev_auth_email:
        if st.is_production:
            raise HTTPException(500, "DEV_AUTH_EMAIL must not be set in production")
        return _user_for_email(db, st.dev_auth_email)

    raise HTTPException(401, "not authenticated")


def require_write(p: Principal = Depends(current_principal)) -> Principal:
    if not p.can_write:
        raise HTTPException(403, "read-only access")
    return p


def require_override(p: Principal = Depends(current_principal)) -> Principal:
    if not p.can_override:
        raise HTTPException(403, "read-only access")
    return p


def require_admin(p: Principal = Depends(current_principal)) -> Principal:
    if p.role != "admin":
        raise HTTPException(403, "admins only")
    return p
