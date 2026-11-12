# ecobee-manager

Web app that groups, schedules and calendar-drives the residential ecobee thermostats in
Grace Bible Church's commercial spaces across several campuses. One FastAPI container +
Postgres + a cloudflared tunnel, deployed as a Komodo stack like stream-scheduler (GHCR
image from CI, compose pasted into Komodo, secrets in its Environment). No Traefik.
Full design: `docs/SPEC.md` — read it before changing behavior.

## Commands

```bash
python -m venv .venv && . .venv/bin/activate && pip install -e '.[dev]'
pytest -q                      # 56 tests, ~8 s
ruff check app tests           # must stay clean
# Local run with 6 simulated thermostats (nothing real touched):
ECOBEE_BACKEND=fake DEV_AUTH_EMAIL=you@example.org \
  ADMIN_EMAILS=you@example.org \
  APP_SECRET_KEY=$(python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())") \
  uvicorn app.main:app --reload
```

## Map

| Path | What |
|---|---|
| `app/resolver.py` | **Pure** scheduling logic: override > calendar > schedule > default, guardrails, `next_change`. No I/O — keep it that way and test it in `tests/test_resolver.py`. |
| `app/models.py` | Campus → Building → Zone (HVAC: thermostats, comfort, schedule) ↔ Room (bookable, own Google calendar; `zone_room` many-to-many). Groups cut across buildings. |
| `app/reconciler.py` | Resolver output vs. what was last sent → batched `set_hold`. `plan()` is read-only. |
| `app/ecobee/backend.py` | `ThermostatBackend` protocol + `EcobeeCloudBackend`. The only code that talks to ecobee. |
| `app/ecobee/fake.py`, `dryrun.py` | Simulated backend for dev/tests; wrapper that logs writes instead of sending. |
| `app/poller.py` | ecobee → `thermostat` table (summary every minute, full fetch on revision change). |
| `app/history.py` | ecobee runtime report → `runtime_sample` (5-min history), every 15 min; backfill + prune. |
| `app/insights.py`, `app/charts.py` | **Pure**: warm-up times, daily runtime, occupancy over history; server-rendered SVG room chart. |
| `app/gcal.py` | Workspace resource list + incremental event sync (service account, domain-wide delegation). |
| `app/auth.py` | Cloudflare Access JWT verification, roles, API keys, dev bypass. |
| `app/api.py` | REST `/api/v1`. The UI reuses these functions — put logic here, not in `web.py`. |
| `app/web.py`, `app/templates/` | Jinja + HTMX UI. Home (`/`, `rooms.html`) and room pages are for people adjusting rooms; configuration lives under `/setup`. Room status comes from `api.zone_status`. |
| `app/jobs.py` | APScheduler: poll, calendar sync, reconcile, alerts. |
| `app/alerts.py` | Google Chat alerts. `find_incidents()` is pure; `check_once()` opens/resolves `alert` rows. |
| `app/migrations/` | Alembic. `init_db()` runs `upgrade head` at startup. |
| `app/spike.py` | `python -m app.spike` — direct real-account checks (bypasses DRY_RUN). |
| `docker-compose*.yml`, `.github/workflows/ci.yml` | Local compose, Cloudflare tunnel overlay, Komodo stack; CI publishes `ghcr.io/grace-bible/ecobee-manager`. |

## Rules that must not break

- **Every write is a self-expiring `dateTime` hold** ending `HOLD_OVERLAP` (5 min) after the
  resolver's next change (so there's no gap before the next reconcile), capped at
  `HOLD_HORIZON_MINUTES` (240). Never write `hvacMode`, programs or comfort
  settings: if the app dies, thermostats must fall back to their own program within hours.
  "Off" = widest band the zone's guardrails allow.
- **Only `app/ecobee/backend.py` may import `pyecobee`.** The login is unofficial (ecobee's
  web OAuth client, same as Home Assistant 2026.3+). `python-ecobee-api` is pinned to 0.4.4;
  we also call its private `_request_with_refresh`. Re-check both on any version bump.
- ecobee API: temperatures are integer **tenths of °F** (but the runtime report gives °F
  decimals, in the thermostat's local time); `zoneOccupancy` is 0 forever on units with no
  sensor, so only trust it once it has been 1; max **25 thermostats per
  selection** (batch with `_chunks`); don't full-fetch a thermostat more than every 3 min.
- `DRY_RUN=true` is the default. In dry run, `app_hold_source` is prefixed `dry:` so the
  reconciler re-sends for real once dry run is turned off — keep that behavior.
- A change made at the wall is respected until the zone's next transition; the
  reconciler only re-sends on target change or < 15 min before its hold expires.
- Auth: the app verifies `Cf-Access-Jwt-Assertion` itself; never trust a header-only
  email. API keys carry a role + campus ids like people; never grant admin implicitly. `DEV_AUTH_EMAIL` must stay refused when `ENVIRONMENT=production`.
- Times are stored UTC; use `models.aware()` on values read back (SQLite returns naive).
- Secrets (ecobee password, TOTP secret, service-account JSON, `.env`) never go in the repo.

## State of the build (v0.2, Nov 2026)

Done: everything in v0.1 plus Alembic migrations, schedule-block editor (zone + group),
group pages, campus-manager scoping in UI and API (incl. override cancel), users/roles in
Settings, Google Chat alerts, Komodo/cloudflared deploy + CI image publishing.

Spike passed on the real ecobee account (2026-11-12): web login + TOTP, summary/full
fetch, and a 66/78 dateTime hold on GYM 303 that showed the right end time and expired
back to the thermostat's own program on its own. The account has only 5 thermostats
(all gym, 3 offline; one unnamed with no timezone).

Still open:
1. **Rest of the real-account checks:** `spike resume`, token refresh after ≥ 1 h, then the
   app with `ECOBEE_BACKEND=cloud` + `DRY_RUN=true` on one zone. Google (domain-wide
   delegation, resource calendars) hasn't been touched yet.
2. Later: HomeKit local backend behind `ThermostatBackend`, MCP server over `/api/v1`.

## Conventions

- Python 3.12+, SQLAlchemy 2.0 typed models, Pydantic v2, ruff line length 100.
- New behavior in the resolver gets a test first.
- Schema change → edit `app/models.py`, then
  `alembic -c app/migrations/alembic.ini revision --autogenerate -m "…"`; never `create_all`.
- Deploy mirrors stream-scheduler: no reverse proxy, no published ports in Komodo; ingress
  is the `cloudflared` sidecar.
