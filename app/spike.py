"""Milestone 1 spike against the real ecobee account (docs/SPEC.md §11).

Talks to ecobee directly, bypassing DRY_RUN, so use it on one test thermostat:

    python -m app.spike login [--mfa 123456]   # full web login; stores encrypted tokens
    python -m app.spike list                   # summary + full fetch, read-only
    python -m app.spike hold IDENT --heat 66 --cool 78 --minutes 15
    python -m app.spike resume IDENT           # clear the hold → back to its own program

Reads ECOBEE_* / APP_SECRET_KEY / DATABASE_URL from the environment or .env, like the app.
"""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta

from app.config import get_settings
from app.db import init_db
from app.ecobee.backend import AuthRequired, BackendError, EcobeeCloudBackend
from app.models import utcnow
from app.services import load_tokens, save_tokens


def _backend() -> EcobeeCloudBackend:
    st = get_settings()
    if not (st.ecobee_username and st.ecobee_password):
        sys.exit("ECOBEE_USERNAME and ECOBEE_PASSWORD must be set")
    return EcobeeCloudBackend(st.ecobee_username, st.ecobee_password, st.ecobee_totp_secret,
                              load_tokens, save_tokens)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.spike", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    lg = sub.add_parser("login")
    lg.add_argument("--mfa", help="one-time code, if no TOTP secret is configured")
    sub.add_parser("list")
    h = sub.add_parser("hold")
    h.add_argument("identifier")
    h.add_argument("--heat", type=float, required=True)
    h.add_argument("--cool", type=float, required=True)
    h.add_argument("--minutes", type=int, default=15)
    h.add_argument("--tz", default=None)
    r = sub.add_parser("resume")
    r.add_argument("identifier")
    args = ap.parse_args(argv)

    init_db()
    b = _backend()
    try:
        if args.cmd == "login":
            b.login(args.mfa)
            print("login ok; tokens stored")
        elif args.cmd == "list":
            rows = b.summary()
            print(f"{len(rows)} thermostats")
            states = {s.identifier: s for s in b.fetch([r.identifier for r in rows])}
            for row in rows:
                s = states.get(row.identifier)
                print(f"  {row.identifier}  {row.name:<30} "
                      f"{'online ' if row.connected else 'OFFLINE'} "
                      f"{s.current_temp if s else '?'}°F  "
                      f"set {s.heat_setpoint if s else '?'}/{s.cool_setpoint if s else '?'}  "
                      f"mode={s.hvac_mode if s else '?'}  tz={s.timezone if s else '?'}  "
                      f"hold={'yes' if s and s.active_hold else 'no'}")
        elif args.cmd == "hold":
            if args.cool - args.heat < get_settings().min_deadband_f:
                sys.exit(f"keep at least {get_settings().min_deadband_f:g}°F between heat and cool")
            until = utcnow() + timedelta(minutes=args.minutes)
            tz = args.tz or get_settings().default_timezone
            b.set_hold([args.identifier], args.heat, args.cool, until, tz)
            print(f"hold {args.heat:g}/{args.cool:g}°F on {args.identifier} until {until:%H:%M} UTC")
        elif args.cmd == "resume":
            b.resume([args.identifier])
            print(f"resumed program on {args.identifier}")
    except (AuthRequired, BackendError) as err:
        print(f"ecobee error: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
