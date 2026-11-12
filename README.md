# ecobee-manager

Group, schedule and calendar-drive the residential ecobee thermostats in GBC's commercial
spaces. One container + Postgres; web UI and REST API; zones follow Google Workspace room
(resource) calendars. Design: [docs/SPEC.md](docs/SPEC.md).

> **Heads-up:** ecobee closed its developer program. This app logs in the way ecobee's own
> website does (via the pinned `python-ecobee-api`, same as Home Assistant 2026.3+). It's
> unofficial and can break if ecobee changes their login. All of that is isolated in
> `app/ecobee/backend.py`.

## How it behaves

- **Campus → building → zone ↔ room.** A **room** is what people book (its own Google room
  calendar); a **zone** is what the HVAC controls (one or more thermostats). A zone serves
  one or more rooms and is booked when any of them is. Buildings and groups can be
  overridden or scheduled as a whole.
- Each **zone** resolves a target every minute:
  override → room-calendar booking (with lead/tail) → weekly schedule → unoccupied default.
- Targets are sent as **timed holds** that end just after the next change (≤ 4 h). If this app
  stops, holds expire and thermostats fall back to their own on-device program, so set
  each thermostat's program to a safe unoccupied setback once, by hand.
- "Off" means the widest band the zone's guardrails allow, not `hvacMode=off`, so it
  can't get stuck either.
- A change made at the wall stands until the next scheduled transition.
- Event tags on a booking: `#nohvac`, `#hvac:heat=70`, `#hvac:cool=71`.
- **History:** ecobee's 5-minute data is kept for 400 days. Room pages chart it, and
  **Insights** shows heating/cooling hours, how long rooms take to warm up or cool down, and
  rooms conditioned with nobody in them (where a sensor exists).
- Ships in **dry run**: reads are live, writes only go to the audit log. `DRY_RUN=false`
  arms the app; then each campus goes live on its own (Setup → campus → Go live).
- **Closures and special hours** per campus or building replace the weekly schedule on
  those days; a whole-campus override handles a snow day.
- **Roles:** admin, campus manager (setup and schedules on their campuses), operator
  (temporary changes only, on their campuses), viewer.

## Deploy (Komodo, same as stream-scheduler)

- **Image:** pushing to `main` runs `.github/workflows/ci.yml` (ruff, pytest, builds the image,
  boots it against Postgres, then publishes `ghcr.io/grace-bible/ecobee-manager:main`).
  A `v*` tag publishes semver tags and `latest`.
- **Stack:** new Komodo stack, UI-defined compose, paste `docker-compose.komodo.yml`.
  Put the secrets in the stack's **Environment** (`POSTGRES_PASSWORD`, `APP_SECRET_KEY`,
  `ECOBEE_*`, `GOOGLE_SERVICE_ACCOUNT_JSON`, `GOOGLE_IMPERSONATE`, `CF_ACCESS_*`,
  `TUNNEL_TOKEN`, `ADMIN_EMAILS`, `GOOGLE_CHAT_WEBHOOK`). Turn on *Poll for updates* and
  *Auto update*.
- **Ingress:** the `cloudflared` sidecar dials out with `TUNNEL_TOKEN`; give the tunnel a
  public hostname pointing at `http://app:8000` and put an Access application on it.
  No ports are published and no reverse proxy is involved.
- **Schema:** migrations run on every start (`alembic upgrade head`).

Off Komodo: `docker compose up -d` (loopback only) or add
`-f docker-compose.cloudflare.yml` for the tunnel.

## Setup

1. **ecobee account.** Move every managed thermostat under one org-owned ecobee account.
   Turn on authenticator-app MFA and keep the base32 secret for `ECOBEE_TOTP_SECRET`.
2. **Google service account.** Create one in a GCP project, enable the Calendar API and
   Admin SDK API, download its JSON key to `secrets/google-sa.json`. In Admin console →
   Security → API controls → Domain-wide delegation, add its client ID with:
   ```
   https://www.googleapis.com/auth/calendar.readonly,https://www.googleapis.com/auth/admin.directory.resource.calendar.readonly
   ```
   Put the key in `GOOGLE_SERVICE_ACCOUNT_JSON` on one line (`jq -c . key.json`).
   `GOOGLE_IMPERSONATE` must be a user who can see the room calendars and read resources.
3. **Cloudflare.** Create a tunnel (copy its token to `TUNNEL_TOKEN`) with a public
   hostname → `http://app:8000`, and a self-hosted Access application on that hostname;
   copy its AUD tag to `CF_ACCESS_AUD`. For n8n/automation, add a service-token policy and
   create an app API key in Setup → System → API keys (or `POST /api/v1/api-keys` with
   `{"name", "role", "campus_ids"}`). Keys get the same roles as people; give automations
   a campus-manager key for the campuses they need, not an admin key.
4. **Spike first.** Before deploying, on one test thermostat:
   ```bash
   python -m app.spike login     # uses ECOBEE_* from .env; add --mfa 123456 if asked
   python -m app.spike list
   python -m app.spike hold <identifier> --heat 66 --cool 78 --minutes 15
   python -m app.spike resume <identifier>
   ```
5. **Deploy** (above). Then Settings → *Log in to ecobee*. Thermostats appear after the
   first poll. In Setup: add campuses and buildings, import rooms from Google, create zones
   (thermostats + the rooms they serve). Fix anything under Setup → Health checks (Auto
   mode, a flat-setback program on each thermostat). Watch Activity in dry run, set
   `DRY_RUN=false`, then go live one campus at a time. Give campus managers and operators
   their roles under Settings → Users.

## Develop

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
ECOBEE_BACKEND=fake DEV_AUTH_EMAIL=you@example.org ADMIN_EMAILS=you@example.org \
  APP_SECRET_KEY=$(python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())") \
  uvicorn app.main:app --reload
pytest
```

`ECOBEE_BACKEND=fake` gives six simulated thermostats; nothing real is touched.

## API

Everything the UI does is under `/api/v1` (OpenAPI at `/docs`). Examples:

```bash
# Hold a group at 70/74 for 2 hours
curl -X POST $URL/api/v1/overrides -H "Authorization: Bearer emk_…" \
  -H "CF-Access-Client-Id: …" -H "CF-Access-Client-Secret: …" \
  -d '{"target":"group","target_id":3,"mode":"custom","heat":70,"cool":74,"minutes":120}'

# What would the scheduler send right now?
curl $URL/api/v1/plan …
```
