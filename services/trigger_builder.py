"""APScheduler trigger construction for scheduled jobs — extracted from
scheduler_service.py in Phase 3 Slice 6. Pure functions operating on a
job's ``schedule`` dict, zero coupling to anything else in the scheduler.
"""
from datetime import datetime, timezone
from typing import Dict, Optional

from apscheduler.triggers.cron     import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger


def _parse_start_from(schedule: Dict) -> Optional[datetime]:
    """Parse an optional ``schedule["start_from"]`` ISO string into a tz-aware UTC datetime.

    Args:
        schedule: Job schedule dict; only ``start_from`` is read.

    Returns:
        A UTC `datetime` (naive inputs are assumed UTC), or ``None`` if absent
        or unparsable.
    """
    raw = schedule.get("start_from")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _build_trigger(schedule: Dict):
    """Build the APScheduler trigger object for a job's ``schedule`` config.

    Supports four schedule shapes (keyed by ``schedule["type"]``):
      - ``"cron"``: raw crontab string in ``schedule["cron"]`` → `CronTrigger`.
      - ``"interval"``: ``schedule["unit"]``/``schedule["value"]`` (e.g. every
        N hours), optionally anchored to ``start_from`` → `IntervalTrigger`.
      - ``"daily"``: fires once a day at ``schedule["hour"]``/``["minute"]`` UTC.
      - ``"weekly"``: fires weekly on ``schedule["day_of_week"]`` at the given
        hour/minute UTC.

    Args:
        schedule: Job schedule dict (see `services.job_manager` job shape).

    Returns:
        An APScheduler trigger instance. Falls back to an hourly
        `IntervalTrigger` for an unrecognized ``type``.
    """
    stype      = schedule.get("type", "interval")
    start_from = _parse_start_from(schedule)

    if stype == "cron":
        return CronTrigger.from_crontab(schedule["cron"])

    if stype == "interval":
        unit   = schedule.get("unit",  "hours")
        value  = int(schedule.get("value", 1))
        kwargs = {unit: value}
        if start_from:
            kwargs["start_date"] = start_from
        return IntervalTrigger(**kwargs)

    if stype == "daily":
        return CronTrigger(
            hour     = int(schedule.get("hour",   8)),
            minute   = int(schedule.get("minute", 0)),
            timezone = "UTC",
        )

    if stype == "weekly":
        return CronTrigger(
            day_of_week = schedule.get("day_of_week", "mon"),
            hour        = int(schedule.get("hour",   8)),
            minute      = int(schedule.get("minute", 0)),
            timezone    = "UTC",
        )

    return IntervalTrigger(hours=1)
