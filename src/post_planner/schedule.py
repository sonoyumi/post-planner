"""Time: people plan in local time ("Monday 9:00 in Rome"), the database stores UTC seconds.

Daylight saving time is where planners break:
* 29 March 2026, 02:30 does not exist in Rome (clocks jump 02:00 -> 03:00): such a time is refused with a
  clear message instead of silently moving the post;
* 25 October 2026, 02:30 happens twice: the first one (summer time) is used;
* a post repeated "every day at 9:00" stays at 9:00 local time after the clocks change (its UTC time moves).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

REPEATS = {"": "Once", "daily": "Every day", "weekdays": "Mon–Fri", "weekly": "Every week"}


class BadTime(ValueError):
    pass


def parse_local(value: str, tz: ZoneInfo) -> float:
    """'05/10/2026 09:00' (as typed in the panel) or '2026-10-05T09:00' in the team's time zone -> UTC timestamp."""
    text = " ".join(value.strip().split())
    try:
        naive = datetime.strptime(text, "%d/%m/%Y %H:%M") if "/" in text else datetime.fromisoformat(text)
    except ValueError:
        raise BadTime(f"{value!r} is not a date and time (write it as 05/10/2026 09:00)") from None
    if naive.tzinfo is not None:
        naive = naive.replace(tzinfo=None)
    local = naive.replace(tzinfo=tz, fold=0)
    # A time inside the spring-forward gap does not survive a round trip through UTC.
    if local.astimezone(UTC).astimezone(tz).replace(tzinfo=None) != naive:
        raise BadTime(f"{naive:%d/%m/%Y %H:%M} does not exist in {tz.key} (the clocks jump forward); pick another time")
    return local.timestamp()


def to_local(ts: float, tz: ZoneInfo) -> datetime:
    return datetime.fromtimestamp(ts, tz)


def input_value(ts: float | None, tz: ZoneInfo) -> str:
    """UTC timestamp -> the date field of the panel, the European way (the browser's own date widget follows the
    computer's locale and would show 10/03/2026 05:30 PM on an English system)."""
    return to_local(ts, tz).strftime("%d/%m/%Y %H:%M") if ts else ""


def next_time(ts: float, repeat: str, tz: ZoneInfo) -> float | None:
    """The next run of a repeating post, keeping the same local wall-clock time."""
    if not repeat:
        return None
    local = to_local(ts, tz).replace(tzinfo=None)
    step = timedelta(days=7 if repeat == "weekly" else 1)
    candidate = local + step
    if repeat == "weekdays":
        while candidate.weekday() >= 5:
            candidate += timedelta(days=1)
    for _ in range(3):  # if 02:30 does not exist on that day, move by an hour (only possible at night)
        try:
            return parse_local(candidate.isoformat(), tz)
        except BadTime:
            candidate += timedelta(hours=1)
    return None


def week_days(ts: float, tz: ZoneInfo, weeks: int = 1) -> list[datetime]:
    """Monday 00:00 of the week containing ts, and the following days (local)."""
    local = to_local(ts, tz)
    monday = (local - timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return [monday + timedelta(days=i) for i in range(7 * weeks)]
