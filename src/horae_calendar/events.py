# SPDX-License-Identifier: Apache-2.0
"""The event model every calendar source returns (ICS now; CalDAV #3 and Google #4 later).

A source turns what its service gives into `Event`s for one `Window`; the
`calendar.events` tool merges, sorts and serialises them. Keeping the model here, apart
from the ICS code, lets the other sources and the preparation-tasks transform (#5) reuse it:
a CalDAV server and the Google API both answer with iCalendar-shaped data (a UID, a
RECURRENCE-ID per instance, DATE or DATE-TIME values), so `event_id`, `instance_key` and
`Window` apply to them as they are.

**Untrusted text.** Titles, locations and descriptions come from whoever can put an event in
the calendar (an invitation is enough), so they are data. `clean_text` keeps them as **plain
text** made safe for any display: line breaks, control and format characters (bidi,
zero-width) removed and the length capped, with the SDK's `escape_title` / `plain_text` pair.
It does not escape Markdown or Obsidian Tasks syntax, because that depends on the target: a
task title built from an event goes through `intents.task_create`, which escapes it.

**Times.** A timed event's `start` and `end` are RFC 3339 date-times with seconds and the
offset of the window's time zone (`2026-10-26T09:30:00+01:00`), the form the core's
contracts accept. A floating time (no `TZID`, no `Z`) is read in the window's time zone
(the ICS reader first applies a feed's `X-WR-TIMEZONE`, as Google means it). A
local time that does not exist (in a spring-forward gap) uses the offset before the gap, and
an ambiguous one (in a fall-back overlap) is the first of the two, as RFC 5545 says. An
all-day event has `YYYY-MM-DD` dates and an **exclusive** `end` (the day after the last day,
as `DTEND;VALUE=DATE` is).
"""

import datetime as dt
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from horae_sdk import intents
from horae_sdk.tasks import escape_title, plain_text

NAMESPACE = "calendar"
"""The first part of every event id (the plugin's name, by the template's convention)."""
MAX_TITLE = 300
MAX_LOCATION = 300
MAX_DESCRIPTION = 1000
MAX_DAYS = 62
_LINE_BREAKS = re.compile("[\\r\\n\\x85\\u2028\\u2029\\v\\f]+")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

Status = Literal["confirmed", "tentative"]


class WindowError(ValueError):
    """A window parameter that cannot be used (its message names the parameter, not data)."""


def clean_text(raw: object, max_length: int) -> str | None:
    """`raw` as plain, single-line text of at most `max_length` characters; `None` when
    nothing printable is left (or `raw` is not a string)."""
    if not isinstance(raw, str):
        return None
    flat = _LINE_BREAKS.sub(" ", raw)
    if not any(_printable(char) for char in flat):
        return None
    text = plain_text(escape_title(flat, max_length=max_length)).strip()
    return text[:max_length] or None


def _printable(char: str) -> bool:
    return not char.isspace() and unicodedata.category(char)[0] not in "CZ"


@dataclass(frozen=True, slots=True)
class Window:
    """Whole days `[start, end)` in the time zone `tz` (the instance's)."""

    first_day: dt.date
    days: int
    tz: ZoneInfo

    @classmethod
    def build(cls, start: str | None, days: int, timezone: str, now: dt.datetime) -> "Window":
        """From the tool's parameters: `start` (`YYYY-MM-DD`, default: `now`'s day in
        `timezone`), `days` (1 to `MAX_DAYS`) and an IANA `timezone`."""
        if type(days) is not int or not 1 <= days <= MAX_DAYS:
            raise WindowError(f"days must be a whole number from 1 to {MAX_DAYS}")
        try:
            tz = ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise WindowError("timezone is not a known IANA time zone") from None
        if start is None:
            first = now.astimezone(tz).date()
        else:
            if not _DATE.fullmatch(start):
                raise WindowError("start must be a date, YYYY-MM-DD")
            try:
                first = dt.date.fromisoformat(start)
            except ValueError:
                raise WindowError("start must be a real date") from None
        if not dt.date(1971, 1, 1) <= first <= dt.date(9000, 1, 1):
            raise WindowError("start is out of range")
        return cls(first, days, tz)

    @property
    def end_day(self) -> dt.date:
        """The first day after the window."""
        return self.first_day + dt.timedelta(days=self.days)

    @property
    def start(self) -> dt.datetime:
        return self.local(self.first_day)

    @property
    def end(self) -> dt.datetime:
        return self.local(self.end_day)

    def local(self, day: dt.date) -> dt.datetime:
        """Midnight of `day` in the window's time zone (normalised if midnight is skipped)."""
        return normalise(dt.datetime.combine(day, dt.time(), self.tz), self.tz)

    def aware(self, value: dt.datetime) -> dt.datetime:
        """`value` in the window's time zone; a floating (naive) value is read in it."""
        return normalise(value.replace(tzinfo=self.tz) if value.tzinfo is None else value, self.tz)

    def overlaps_timed(self, start: dt.datetime, end: dt.datetime) -> bool:
        """`[start, end)` meets the window; an instant event (`end <= start`) is in it when
        it starts in it."""
        if end <= start:
            return self.start <= start < self.end
        return start < self.end and end > self.start

    def overlaps_days(self, first: dt.date, end: dt.date) -> bool:
        """The days `[first, end)` meet the window's days."""
        return first < self.end_day and end > self.first_day

    def to_json(self) -> dict[str, Any]:
        return {
            "start": rfc3339(self.start),
            "end": rfc3339(self.end),
            "timezone": self.tz.key,
            "days": self.days,
        }


def normalise(value: dt.datetime, tz: ZoneInfo) -> dt.datetime:
    """An aware `value` in `tz`, through UTC: a time in a gap gets a real wall time."""
    return value.astimezone(dt.UTC).astimezone(tz)


def rfc3339(value: dt.datetime) -> str:
    """`2026-10-26T09:30:00+01:00` (seconds always; `+00:00`, never `Z`)."""
    return value.isoformat(timespec="seconds")


def instance_key(value: dt.date | dt.datetime) -> str:
    """The RECURRENCE-ID of an instance as stable text: `20261026` for a date,
    `20261026T083000Z` for an aware date-time (in UTC, so a time zone renamed or rewritten by
    the server keeps the key), `20261026T093000` for a floating one."""
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            return value.strftime("%Y%m%dT%H%M%S")
        return value.astimezone(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    return value.strftime("%Y%m%d")


def event_id(uid: str, instance: str | None) -> str:
    """The stable id of an event or of one instance of a recurring event:
    `intents.fingerprint("calendar", UID)` or `intents.fingerprint("calendar", UID, key)`,
    where `key` is the `instance_key` of its RECURRENCE-ID (its original start). A moved
    instance keeps its id; an instance of a recurring event never shares one with another."""
    return (
        intents.fingerprint(NAMESPACE, uid, instance)
        if instance
        else intents.fingerprint(NAMESPACE, uid)
    )


@dataclass(frozen=True, slots=True)
class Event:
    """One event, or one instance of a recurring event, in a window."""

    id: str
    calendar: str
    title: str
    start: dt.datetime | dt.date
    end: dt.datetime | dt.date
    all_day: bool
    location: str | None = None
    description: str | None = None
    status: Status = "confirmed"
    recurring: bool = False

    def sort_key(self, window: Window) -> tuple[dt.datetime, int, dt.datetime, str]:
        """Chronological; an all-day event before the timed ones of its first day."""
        if isinstance(self.start, dt.datetime) and isinstance(self.end, dt.datetime):
            return (self.start.astimezone(dt.UTC), 1, self.end.astimezone(dt.UTC), self.id)
        start = window.local(_day(self.start)).astimezone(dt.UTC)
        return (start, 0, window.local(_day(self.end)).astimezone(dt.UTC), self.id)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "calendar": self.calendar,
            "title": self.title,
            "start": _when(self.start),
            "end": _when(self.end),
            "all_day": self.all_day,
            "location": self.location,
            "description": self.description,
            "status": self.status,
            "recurring": self.recurring,
        }


def _day(value: dt.date | dt.datetime) -> dt.date:
    return value.date() if isinstance(value, dt.datetime) else value


def _when(value: dt.date | dt.datetime) -> str:
    return rfc3339(value) if isinstance(value, dt.datetime) else value.isoformat()
