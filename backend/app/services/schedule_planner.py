"""Pure helpers mapping a weekly :class:`~app.models.schedule.Schedule` to
concrete wear dates and reminder times.

This module is the **single source of truth** for the ``notify_day_before``
date-shift convention. Both the notification worker
(``check_scheduled_notifications`` / ``process_scheduled_notification``) and the
weekly planner (``PlanService``) rely on it so that the two never drift apart.

Convention:
- ``Schedule.day_of_week`` is the weekday (0=Monday) on which the outfit is
  *worn*.
- If ``notify_day_before`` is ``True`` the reminder fires the **evening before**
  the wear date; otherwise it fires on the wear date itself.

All functions are pure (no DB or network I/O) and deterministic given their
inputs, which makes them straightforward to unit-test across timezones.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.models.schedule import Schedule


@dataclass(frozen=True)
class ScheduleOccurrence:
    """A single upcoming firing of a schedule, expanded to concrete dates."""

    schedule: Schedule
    wear_date: date  # day to WEAR the outfit (weekday == schedule.day_of_week)
    reminder_at: datetime  # tz-aware instant the reminder fires
    notify_day_before: bool


def reminder_date_for_wear_date(wear_date: date, notify_day_before: bool) -> date:
    """The local date a reminder fires for an outfit worn on ``wear_date``."""
    if notify_day_before:
        return wear_date - timedelta(days=1)
    return wear_date


def reminder_datetime_for_wear_date(
    schedule: Schedule, wear_date: date, tz: ZoneInfo
) -> datetime:
    """The tz-aware datetime the reminder fires for ``wear_date``."""
    reminder_date = reminder_date_for_wear_date(wear_date, schedule.notify_day_before)
    return datetime.combine(reminder_date, schedule.notification_time, tzinfo=tz)


def wear_date_for_reminder_today(schedule: Schedule, reminder_date: date) -> date:
    """Given the date a reminder fires, the date the outfit is worn.

    Inverse of :func:`reminder_date_for_wear_date`. This matches the worker's
    inline ``target_date`` computation in ``process_scheduled_notification``.
    """
    if schedule.notify_day_before:
        return reminder_date + timedelta(days=1)
    return reminder_date


def fires_on(schedule: Schedule, now_local: datetime) -> datetime | None:
    """Return the reminder datetime if this schedule should fire on
    ``now_local``'s date, else ``None``.

    Reproduces the worker's ``day_match`` exactly: the schedule fires when the
    wear date implied by today's reminder lands on a weekday equal to
    ``schedule.day_of_week``. The caller is responsible for the time-of-day
    window check (and for filtering to enabled schedules).
    """
    reminder_date = now_local.date()
    wear_date = wear_date_for_reminder_today(schedule, reminder_date)
    if wear_date.weekday() != schedule.day_of_week:
        return None
    return datetime.combine(
        reminder_date, schedule.notification_time, tzinfo=now_local.tzinfo
    )


def upcoming_schedule_map(
    schedules: list[Schedule],
    *,
    today: date,
    tz: ZoneInfo,
    days: int,
) -> dict[date, list[ScheduleOccurrence]]:
    """Expand enabled schedules into occurrences keyed by wear date.

    For each wear date in ``[today, today + days)`` collect every enabled
    schedule whose ``day_of_week`` matches that date's weekday. Multiple
    schedules may cover the same day (the per-day uniqueness constraint was
    dropped), so each date maps to a list.
    """
    result: dict[date, list[ScheduleOccurrence]] = {}
    for offset in range(days):
        wear_date = today + timedelta(days=offset)
        occurrences: list[ScheduleOccurrence] = []
        for schedule in schedules:
            if not schedule.enabled:
                continue
            if schedule.day_of_week != wear_date.weekday():
                continue
            occurrences.append(
                ScheduleOccurrence(
                    schedule=schedule,
                    wear_date=wear_date,
                    reminder_at=reminder_datetime_for_wear_date(schedule, wear_date, tz),
                    notify_day_before=schedule.notify_day_before,
                )
            )
        result[wear_date] = occurrences
    return result
