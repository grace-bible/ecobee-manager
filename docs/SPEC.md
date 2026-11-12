# ecobee-manager — Spec

Status: draft v0.1 (Nov 2026) · Owner: Technical Operations, Grace Bible Church

## 1. Problem

GBC runs many residential ecobee thermostats in commercial spaces across several
campuses. ecobee's app can't group thermostats, has no concept of building usage,
and makes multi-room changes a one-at-a-time chore. Rooms are already booked as
Google Workspace resource calendars, but HVAC doesn't follow those bookings.

## 2. Goals

- One web UI for every thermostat across all campuses.
- Group thermostats into **zones**, and zones into **groups** (campus, building,
  free-form tags). Act on any group in one click.
- Link a zone to a **Google Workspace resource calendar** so the room is
  conditioned before a booking and set back after it.
- Weekly schedules per zone/group, with guardrails (min/max setpoints).
- Fail safe: if this app or ecobee's cloud is down, thermostats fall back to a
  sane on-device program. Nothing the app sets can get "stuck".
- Self-contained: no Home Assistant, no n8n required. n8n/AI can integrate later
  through the same API the UI uses.

### Non-goals (v1)

- Editing ecobee's on-device programs/comfort settings.
- Local (HomeKit) control. Kept open as a future backend (§10).
- Energy reporting beyond runtime/state history.

## 3. Architecture

```
Cloudflare Access ──► cloudflared (tunnel sidecar) ──► ecobee-manager (FastAPI)
                                                     │  ├─ web UI (Jinja + HTMX)
                                                     │  ├─ REST API  /api/v1
                                                     │  ├─ poller     (ecobee → cache)
                                                     │  ├─ calendar sync (Google → events)
                                                     │  └─ reconciler (desired vs actual → ecobee)
                                                     ▼
                                                  Postgres
```

One container plus Postgres and a `cloudflared` tunnel, deployed as a Komodo stack
the same way as stream-scheduler: CI publishes `ghcr.io/grace-bible/ecobee-manager:main`,
the stack's compose (`docker-compose.komodo.yml`) is pasted into Komodo with secrets in
its Environment, and Komodo polls for new images. No reverse proxy. Schema changes ship
as Alembic migrations that run at startup. Background
jobs run in-process (APScheduler) — a single replica is assumed; the reconciler
takes a Postgres advisory lock so an accidental second replica is harmless.

### 3.1 ecobee backend

- Library: `python-ecobee-api` pinned (currently 0.4.4). It logs in through
  ecobee's own web OAuth flow (PKCE, ecobee's web client ID) — the same path
  Home Assistant uses since 2026.3. **This is unofficial.** It is isolated
  behind `app/ecobee/backend.py` so it can be swapped (HomeKit, or an official
  key if one becomes available).
- Account: a dedicated org-owned ecobee account (e.g. `facilities@`) that owns
  every managed thermostat.
- MFA: TOTP on that account. The TOTP secret is stored as a stack secret so
  the app can complete a forced re-login unattended (`pyotp`). Steady state uses
  the refresh token; full login only happens when the refresh token is rejected.
- Tokens persist in Postgres (`ecobee_credentials`), encrypted at rest with
  `APP_SECRET_KEY` (Fernet).
- Polling: `thermostatSummary` every 60 s (cheap, returns revision strings);
  full `thermostat` fetch only for thermostats whose runtime/settings revision
  changed, and no thermostat more often than every 3 min (ecobee guidance).
- Writes are batched: one `setHold` with a comma-separated `selectionMatch`
  for all thermostats that share the same target.

### 3.2 Holds, never programs

Every change the app makes is a `setHold` with `holdType: dateTime` and an
explicit end time: 5 minutes after the zone's next change (the next hold replaces it at
the change, so there's no gap between reconciles), ≤ 4 h. If the app dies, the
hold expires and the thermostat returns to its own program. The on-device
program at every site should be a flat, safe **unoccupied setback** (configured
once by hand) — that's the fallback.

## 4. Data model

| Table | Purpose / key fields |
|---|---|
| `campus` | name, timezone |
| `comfort_profile` | name, campus_id (optional = every campus), occupied/unoccupied heat & cool, min/max, lead/tail — shared by zones |
| `special_day` | campus_id, building_id (optional), first/last date, closed or special hours (start/end, mode), `closed_mode` (empty/off), `ignore_bookings`, label |
| `building` | campus_id, name, Workspace `google_building_id` (from the room import) |
| `room` | campus_id, building_id, name, **`calendar_id`** (Google resource calendar, unique), capacity — what people book |
| `zone_room` | many-to-many: a zone serves one or more rooms (usually a room has one zone; a hall on two units has two) |
| `thermostat` | ecobee `identifier`, name, model, campus_id, last-known state (temp, setpoints, mode, running equipment, connected), `last_seen` |
| `zone` | what the HVAC controls: name, campus_id, building_id, `lead_minutes`, `tail_minutes`, occupied/unoccupied heat & cool setpoints, `min_setpoint`, `max_setpoint`, `enabled`. Its rooms' calendars drive it |
| `zone_thermostat` | many-to-many (a big room can have several thermostats; a thermostat belongs to one zone) |
| `group` / `group_zone` | groupings of zones that cut across buildings ("All kids rooms", "Sunday morning") |
| `schedule_block` | owner (zone, group or building), weekday mask, start, end, mode: `occupied` / `unoccupied` / `off` / custom setpoints |
| `override` | target (thermostat/zone/group/building), setpoints or mode, `starts_at`, `ends_at`, created_by, reason |
| `calendar_event` | calendar_id, Google event id, start/end, title, status, `hvac_tag` parsed from title/description |
| `calendar_sync_state` | calendar_id, `sync_token`, last success/error |
| `audit_log` | who, when, what, source (`ui`/`api`/`calendar`/`schedule`), before/after |
| `app_user` | email (from Cloudflare Access), role: `admin` / `campus_manager` (setup + schedules on its campus ids) / `operator` (overrides only, on its campus ids) / `viewer` |
| `api_key` | hashed key, name, role + campus ids (same roles as `app_user`; viewer by default) — for n8n/automation |
| `runtime_sample` | thermostat, 5-min interval (UTC), indoor/outdoor temp, humidity, setpoints, heating/cooling/fan seconds, occupancy — from ecobee's runtime report, kept 400 days |
| `ecobee_credentials` | encrypted refresh/access token, last login |

## 5. Scheduling: how a zone's target is decided

Every minute the **resolver** computes each zone's desired state *for now* by
taking the highest-priority active source:

1. **Manual override** (thermostat > zone > group > building > campus, newest wins within a
   level). A campus override (snow day) beats bookings and schedules.
2. **Calendar occupancy** — any confirmed event on any of the zone's rooms' calendars
   overlapping `[now + lead, now - tail]` → `occupied` (an event booked in several of
   the zone's rooms counts once). All-day/multi-day bookings count only with `#hvac:on`.
   Skipped on a special day marked "ignore bookings".
3. **Special day** (`special_day`) — a closure or special hours on given dates for the
   campus or one building (building wins). Replaces the weekly schedule on those days.
4. **Schedule block** — zone blocks, then group blocks, then building blocks
5. **Default** → `unoccupied` setpoints for that zone

**Going live.** `DRY_RUN` is a master switch. With it off, each campus goes live on its own
(Setup → campus → Go live); a zone can follow its campus or be set live / dry run itself.
Non-live zones' writes are recorded only.

**At scale.** Comfort profiles (`comfort_profile`) hold shared setpoints and lead/tail;
zones using one follow it (values are copied to the zone, so everything reads the zone).
Schedule blocks can be seasonal (`season_start`/`season_end`, MM-DD, may wrap New Year).
Rooms have booking rules (`drives_hvac`, `min_booking_minutes`, `ignore_tentative`).
Zones with `lead_auto` get their lead set nightly from the last 30 days of time-to-target
(80th percentile + 10 min, 15–180). Each campus can have its own Google Chat webhook for its
alerts. Buildings and zones have a week view showing what each zone will do and why.

**Device checks.** The app relies on thermostats being in Auto (holds set heat and cool),
and on each device's own program being a flat setback (it's the fallback if the app stops).
A managed thermostat not in Auto raises an alert; both show under Setup → Health checks.

Then **guardrails** clamp setpoints to the zone's `[min, max]`, enforce a
minimum 4 °F heat/cool deadband, and the **reconciler** diffs desired vs. the
thermostat's current hold. Only differences are written, batched per identical
target. Each write gets an audit row with its source.

Calendar keyword tags (in event title or description, case-insensitive):

- `#nohvac` — ignore this event
- `#hvac:cool=72` / `#hvac:heat=68` — event-specific setpoint (still clamped)

## 6. Google Calendar integration

- A Google Cloud **service account with domain-wide delegation**, impersonating
  a designated Workspace user (`GOOGLE_IMPERSONATE`, e.g. a facilities account
  that can see all resource calendars).
- Scopes: `calendar.readonly` (events) and
  `admin.directory.resource.calendar.readonly` (to list resources so a zone's
  calendar can be picked from a dropdown, grouped by building).
- Sync: `events.list` with `singleEvents=true`, a 14-day window, and stored
  `syncToken` for incremental updates every 2 min. A `410 Gone` resets the token
  and does a full re-sync. Push notifications (`events.watch`) can be added
  later; polling is plenty for HVAC.
- Only `confirmed` events count. Declined-by-resource events are ignored.

## 7. Auth & access

- Cloudflare Zero Trust Access in front. The app **also** verifies
  `Cf-Access-Jwt-Assertion` against `https://<team>.cloudflareaccess.com/cdn-cgi/access/certs`
  with the application's AUD tag — so bypassing Cloudflare doesn't bypass auth.
- Identity = JWT `email`. Unknown emails get `viewer` if their domain is in
  `ALLOWED_EMAIL_DOMAINS`, otherwise 403. Admins bootstrap from `ADMIN_EMAILS`.
- Machine access (n8n, MCP): Cloudflare Access service token **and** an app API
  key (`Authorization: Bearer emk_…`), scoped read/write.
- `DEV_AUTH_EMAIL` bypass for local development only; refused if
  `ENVIRONMENT=production`.

## 8. UI (v1)

- **Overview**: every thermostat grouped by campus → zone; current temp,
  setpoints, mode, what's running, connectivity, active source (badge:
  override / calendar / schedule / default), live-refresh via HTMX polling.
- **Bulk bar**: select zones or a group → set hold (setpoints + until), resume,
  set mode.
- **Zones**: assign thermostats, pick resource calendar, setpoints, lead/tail,
  guardrails; preview "next 7 days" timeline resolved from calendar + schedule.
- **Groups / Schedules / Overrides / Audit log / Settings** (ecobee account
  status + re-login with MFA, Google sync status, users).

## 9. Alerts

Direct Google Chat webhook (no n8n dependency) for: thermostat offline > 15 min,
zone not reaching setpoint 60 min into an occupied period (more than 3 °F outside the
band), ecobee auth failing, calendar sync failing > 15 min, and any alert the ecobee itself
raises (equipment faults, temperature alerts). Debounced: each incident is
an `alert` row, with one message when it opens and one when it resolves. Open alerts also
show as a banner in the UI and on Settings. Implemented in `app/alerts.py`.

## 9a. History and insights

`app/history.py` pulls ecobee's runtime report (5-minute intervals; ecobee uploads about
every 15 min) into `runtime_sample`: 31 days of backfill on first sight of a thermostat,
then a re-read of the last 2 h every 15 min. Each room page shows a 24 h / 7 day chart
(indoor, target band, outdoor, heating/cooling strip) plus, for the last week: typical and
slowest time to reach a new target (compared with the room's booking lead time), daily
heating/cooling hours, and hours conditioned with nobody detected (rooms with an occupancy
sensor only). `/insights` compares rooms. API: `GET /api/v1/zones/{id}/history`,
`/insights`.

## 10. Future

- HomeKit local backend via per-campus gateway (aiohomekit) behind the same
  `ThermostatBackend` interface.
- MCP server / n8n nodes over `/api/v1`.
- Planning Center Calendar as an alternate occupancy source.
- Optional: publish effective schedule to a read-only "HVAC" Google calendar.

## 11. Milestones

1. **Spike** — log in, list thermostats, set + clear a hold on one test unit.
2. **Read-only fleet view** — poller, campuses, zones, overview page.
3. **Bulk control** — overrides, groups, audit log.
4. **Calendar** — resource picker, sync, resolver/reconciler live on one campus.
5. **Schedules, guardrails, alerts** — then roll out campus by campus.
