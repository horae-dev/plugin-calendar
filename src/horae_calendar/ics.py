# SPDX-License-Identifier: Apache-2.0
"""Parse an iCalendar feed and expand its events over a `Window`.

Parsing is `icalendar`'s; recurrence is `recurring-ical-events`' (RRULE, RDATE, EXDATE,
RECURRENCE-ID overrides including `RANGE=THISANDFUTURE`, VTIMEZONE definitions, the
`X-WR-TIMEZONE` that Google exports carry). This module adds what Horae needs on top:

- the occurrences are taken per series from the library's lazy iterators, a day around the
  window (so a floating time or a far time zone cannot fall off an edge), then each is
  checked against the window exactly, in the window's time zone (`Window.overlaps_*`);
- a cancelled instance (`STATUS:CANCELLED` on an override) or series is left out;
- each event gets a stable id (`events.event_id`): its UID, plus the RECURRENCE-ID of the
  instance for a recurring event, so a moved instance keeps its id. An event with no UID
  gets one from a digest of its content (stable while it does not change);
- text fields are cleaned (`events.clean_text`);
- the work is bounded: `MAX_OCCURRENCES` instances per feed (cancelled ones included) or the
  feed fails with `too_many_events`; a series that recurs every second or minute is skipped
  (its expansion from DTSTART would take too long); one malformed event (no DTSTART, a
  broken rule), series or instance is skipped and counted, never fatal.
"""

import datetime as dt
import hashlib
import itertools
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import icalendar
import recurring_ical_events
from icalendar.cal import Component

from horae_calendar.events import (
    MAX_DESCRIPTION,
    MAX_LOCATION,
    MAX_TITLE,
    Event,
    Status,
    Window,
    clean_text,
    event_id,
    instance_key,
)

MAX_OCCURRENCES = 2000
"""Instances generated for one feed over a window, cancelled ones included."""
MAX_COMPONENTS = 50_000
"""VEVENTs in one feed (a 10 MiB feed holds far fewer real events)."""
UNTITLED = "(untitled)"
_REFUSED_FREQ = {"SECONDLY", "MINUTELY"}
_MARGIN = dt.timedelta(days=1)


class IcsError(Exception):
    """A feed that cannot be read at all; `reason` is a fixed code (`parse_failed`,
    `not_icalendar`, `too_many_events`), never the feed's text."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class Expansion:
    """The events of one feed in a window, and how many series or instances were skipped
    (malformed, or recurring too often)."""

    events: list[Event] = field(default_factory=list)
    skipped: int = 0


def parse(data: bytes) -> icalendar.Calendar:
    """`data` as a calendar (`IcsError` when it is not one)."""
    head = data.lstrip(b"\xef\xbb\xbf \t\r\n")[:15].upper()
    if not head.startswith(b"BEGIN:VCALENDAR"):
        raise IcsError("not_icalendar")
    try:
        calendar = icalendar.Calendar.from_ical(data)
    except Exception:
        raise IcsError("parse_failed") from None
    if not isinstance(calendar, icalendar.Calendar):  # pragma: no cover (from_ical's contract)
        raise IcsError("parse_failed")
    return calendar


def expand(data: bytes, calendar: str, window: Window) -> Expansion:
    """The events of the feed `data` (named `calendar`) that meet `window`."""
    parsed = parse(data)
    result = Expansion()
    events = [c for c in parsed.walk("VEVENT")]
    if len(events) > MAX_COMPONENTS:
        raise IcsError("too_many_events")
    recurring = _recurring_uids(events)
    refused = _drop_unusable(parsed)
    result.skipped += refused
    try:
        query = recurring_ical_events.of(parsed, skip_bad_series=True)
    except Exception:
        raise IcsError("parse_failed") from None
    lower = query.to_datetime(window.first_day - _MARGIN)
    upper = query.to_datetime(window.end_day + _MARGIN)
    generated = 0
    for series in query.series:
        occurrences = _occurrences(series, lower, upper)
        while True:
            try:
                occurrence = next(occurrences)
            except StopIteration:
                break
            except Exception:  # a malformed series: skip the rest of it
                result.skipped += 1
                break
            generated += 1
            if generated > MAX_OCCURRENCES:
                raise IcsError("too_many_events")
            try:
                event = _event(occurrence, calendar, window, recurring)
            except Exception:  # a malformed instance
                result.skipped += 1
                continue
            if event is not None:
                result.events.append(event)
    return result


def _occurrences(series: Any, lower: Any, upper: Any) -> Iterator[icalendar.Event]:
    for occurrence in itertools.islice(series.between(lower, upper), MAX_OCCURRENCES + 1):
        yield occurrence.as_component(False)


def _recurring_uids(components: list[Component]) -> set[str]:
    """The UIDs of recurring events: a rule, extra dates or an override."""
    return {
        str(component.get("UID"))
        for component in components
        if any(key in component for key in ("RRULE", "RDATE", "RECURRENCE-ID"))
    }


def _drop_unusable(calendar: icalendar.Calendar) -> int:
    """Remove the VEVENTs with no readable DTSTART, or whose rule recurs every second or
    minute or cannot be read; how many."""
    dropped = 0
    for container in [calendar, *calendar.walk()]:
        kept = []
        for sub in container.subcomponents:
            if sub.name == "VEVENT" and (not _has_start(sub) or _too_fast(sub)):
                dropped += 1
                continue
            kept.append(sub)
        container.subcomponents = kept
    return dropped


def _has_start(component: Component) -> bool:
    try:
        return isinstance(component.decoded("DTSTART"), dt.date)
    except Exception:
        return False


def _too_fast(component: Component) -> bool:
    try:
        return _fast_rule(component)
    except Exception:  # a broken RRULE: the series cannot be expanded
        return True


def _fast_rule(component: Component) -> bool:
    rules = component.get("RRULE")
    for rule in rules if isinstance(rules, list) else [rules]:
        if rule is None:
            continue
        frequencies = rule.get("FREQ") if hasattr(rule, "get") else None
        if any(str(f).upper() in _REFUSED_FREQ for f in frequencies or []):
            return True
    return False


def _event(
    component: icalendar.Event, calendar: str, window: Window, recurring: set[str]
) -> Event | None:
    """One occurrence as an `Event`, or `None` when it is cancelled or outside the window."""
    if str(component.get("STATUS", "")).upper() == "CANCELLED":
        return None
    start = component.decoded("DTSTART")
    end = component.decoded("DTEND") if "DTEND" in component else None
    if isinstance(start, dt.datetime):
        begin = window.aware(start)
        finish = window.aware(end) if isinstance(end, dt.datetime) else begin
        if not window.overlaps_timed(begin, max(begin, finish)):
            return None
        when: tuple[dt.date | dt.datetime, dt.date | dt.datetime] = (begin, max(begin, finish))
        all_day = False
    elif isinstance(start, dt.date):
        last = end if isinstance(end, dt.date) and not isinstance(end, dt.datetime) else None
        stop = last if last is not None and last > start else start + dt.timedelta(days=1)
        if not window.overlaps_days(start, stop):
            return None
        when, all_day = (start, stop), True
    else:  # pragma: no cover (the library always gives a date or a date-time)
        return None
    uid, instance = _identity(component, recurring)
    return Event(
        id=event_id(uid, instance),
        calendar=calendar,
        title=clean_text(str(component.get("SUMMARY", "")), MAX_TITLE) or UNTITLED,
        start=when[0],
        end=when[1],
        all_day=all_day,
        location=clean_text(str(component.get("LOCATION", "")), MAX_LOCATION),
        description=clean_text(str(component.get("DESCRIPTION", "")), MAX_DESCRIPTION),
        status=_status(component),
        recurring=instance is not None,
    )


def _identity(component: icalendar.Event, recurring: set[str]) -> tuple[str, str | None]:
    """The UID (or a content digest when there is none) and, for an instance of a recurring
    event, the key of its RECURRENCE-ID."""
    raw_uid = component.get("UID")
    uid = str(raw_uid) if raw_uid is not None and str(raw_uid).strip() else None
    instance = None
    if uid is not None and uid in recurring and "RECURRENCE-ID" in component:
        instance = instance_key(component.decoded("RECURRENCE-ID"))
    if uid is None:
        digest = hashlib.sha256(component.to_ical()).hexdigest()[:16]
        uid = f"no-uid-{digest}"
    return uid, instance


def _status(component: icalendar.Event) -> Status:
    return "tentative" if str(component.get("STATUS", "")).upper() == "TENTATIVE" else "confirmed"
