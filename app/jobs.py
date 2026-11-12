"""Background jobs, in-process. Single replica assumed (reconcile also takes a PG lock)."""

import logging
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler

from app.config import get_settings

log = logging.getLogger(__name__)
_scheduler: BackgroundScheduler | None = None


def _safe(fn):
    def run():
        try:
            log.debug("%s → %s", fn.__name__, fn())
        except Exception:
            log.exception("job %s failed", fn.__name__)
    run.__name__ = fn.__name__
    return run


def start() -> None:
    global _scheduler
    from app import alerts, gcal, history, lead, poller, reconciler

    st = get_settings()
    now = datetime.now(UTC)
    _scheduler = BackgroundScheduler(timezone="UTC",
                                     job_defaults={"coalesce": True, "max_instances": 1})
    # Poll right away at boot; sync and reconcile follow on their intervals.
    _scheduler.add_job(_safe(poller.poll_once), "interval", seconds=st.ecobee_poll_seconds,
                       id="poll", next_run_time=now)
    _scheduler.add_job(_safe(gcal.sync_all), "interval", seconds=st.calendar_sync_seconds,
                       id="calendar", next_run_time=now)
    _scheduler.add_job(_safe(reconciler.reconcile_once), "interval",
                       seconds=st.reconcile_seconds, id="reconcile")
    _scheduler.add_job(_safe(alerts.check_once), "interval", seconds=60, id="alerts")
    _scheduler.add_job(_safe(history.sync_once), "interval",
                       minutes=st.history_sync_minutes, id="history",
                       next_run_time=now + timedelta(seconds=30))
    _scheduler.add_job(_safe(lead.learn_once), "interval", hours=24, id="lead",
                       next_run_time=now + timedelta(minutes=10))
    _scheduler.start()
    log.info("background jobs started (dry_run=%s)", st.dry_run)


def stop() -> None:
    if _scheduler:
        _scheduler.shutdown(wait=False)
