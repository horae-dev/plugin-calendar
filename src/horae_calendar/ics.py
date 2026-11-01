# SPDX-License-Identifier: Apache-2.0
"""Parse an iCalendar feed and expand its events over a `Window`.

Parsing is `icalendar`'s; recurrence is `recurring-ical-events`' (RRULE, RDATE, EXDATE,
RECURRENCE-ID overrides including `RANGE=THISANDFUTURE`, VTIMEZONE definitions, the
`X-WR-TIMEZONE` that Google exports carry). This module adds what Horae needs on top:

- the occurrences are taken per series from the library's lazy iterators, a day around the
  window (so a floating time or a far time zone cannot fall off an edge), then each is
  checked against the window exactly, in the window's time zone (`Window.overlaps_*`);
- a cancelled instance (`STATUS:CANCELLED` on an override) or series is left out;
- each event gets a stable id (`events.event_id`): its UID, plus the **original start** of
  the instance for a recurring event (its RECURRENCE-ID), so a moved instance keeps its id,
  and every instance after a `RANGE=THISANDFUTURE` override keeps its own. Ids are unique
  within a feed. An event with no UID gets one from a digest of its content, without the
  properties a server rewrites on every export (`DTSTAMP`, `SEQUENCE`, `LAST-MODIFIED`,
  `CREATED`); two UID-less events with the same content are one event;
- text fields are cleaned (`events.clean_line`, `events.clean_text`);
- a `DATE` `UNTIL` on a timed series is read as the whole of that day in the series' time
  zone (RFC 5545 asks for a date-time there; calendar apps mean "until that day included");
- a `TZID` that is neither an IANA name nor defined by the feed's VTIMEZONEs is read as a
  floating time (in the window's zone) and counted in `warnings`.

**Bounds.** A feed is untrusted, so its expansion is bounded in instances and in time:

- at most `MAX_PER_SERIES` events per series and `MAX_PER_FEED` per feed (the earliest are
  kept, the series cut short are counted in `truncated`); cancelled instances and the
  margin days do not count;
- `expand` is meant to run in a worker thread with a wall-clock `budget_s` (5 s by default):
  when it runs out, the series not yet expanded are counted in `truncated`;
- each series may take at most `MAX_RULE_STEPS` steps of `dateutil`'s rule iterator
  (`_guarded`): a rule that cannot reach the window cheaply (one that never matches, such as
  `FREQ=DAILY;BYMONTH=2;BYMONTHDAY=30`, or a fast one from a distant `DTSTART`) is skipped
  and counted in `skipped` instead of spinning to the year 9999;
- a series that recurs every second or minute is skipped; one malformed event (no DTSTART,
  a broken rule), series or instance is skipped and counted, never fatal.

A feed with something skipped or truncated is not `complete` (`Expansion.complete`).
"""

import contextlib
import datetime as dt
import hashlib
import itertools
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from typing import Any

import icalendar
import recurring_ical_events
from dateutil import rrule as _rrule
from icalendar.cal import Component

from horae_calendar.events import (
    MAX_DESCRIPTION,
    MAX_LOCATION,
    MAX_TITLE,
    Event,
    Status,
    Window,
    clean_line,
    clean_text,
    event_id,
    instance_key,
)

MAX_PER_SERIES = 500
"""Events kept from one series (the earliest)."""
MAX_PER_FEED = 2000
"""Events kept from one feed (the earliest)."""
MAX_GENERATED = 10 * MAX_PER_SERIES
"""Occurrences generated for one series, cancelled and margin ones included, before it is
cut short."""
MAX_RULE_STEPS = 100_000
"""Steps of `dateutil`'s rule iterator for one series (about 0.05 s): 270 years of a daily
rule, 11 of an hourly one, from its DTSTART to the window."""
BUDGET_S = 5.0
"""Wall-clock time for expanding one feed."""
MAX_COMPONENTS = 50_000
"""VEVENTs in one feed (a 10 MiB feed holds far fewer real events)."""
UNTITLED = "(untitled)"
_REFUSED_FREQ = {"SECONDLY", "MINUTELY"}
_MARGIN = dt.timedelta(days=1)
_VOLATILE = {"DTSTAMP", "SEQUENCE", "LAST-MODIFIED", "CREATED"}
_TIME_PROPERTIES = ("DTSTART", "DTEND", "RECURRENCE-ID")


class IcsError(Exception):
    """A feed that cannot be read at all; `reason` is a fixed code (`parse_failed`,
    `not_icalendar`, `too_many_events`), never the feed's text."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class Expansion:
    """The events of one feed in a window, and what could not be returned in full:
    `skipped` series or instances (malformed, too frequent, too costly to reach the window),
    `truncated` series (cut by a cap or by the time budget), and `warnings` (events kept
    with a caveat: an unknown time zone read as floating)."""

    events: list[Event] = field(default_factory=list)
    skipped: int = 0
    truncated: int = 0
    warnings: int = 0

    @property
    def complete(self) -> bool:
        """Every event of the feed in the window is in `events`."""
        return not self.skipped and not self.truncated


# --- the guard on dateutil's rule iterator ------------------------------------------------


class _Exhausted(Exception):
    """A series ran out of rule steps (`deadline` False) or the feed out of time (True)."""

    def __init__(self, *, deadline: bool) -> None:
        super().__init__("deadline" if deadline else "steps")
        self.deadline = deadline


@dataclass
class _Guard:
    deadline: float
    clock: Callable[[], float]
    left: int = MAX_RULE_STEPS

    def tick(self) -> None:
        self.left -= 1
        if self.left < 0:
            raise _Exhausted(deadline=False)
        if self.left % 512 == 0 and self.clock() > self.deadline:
            raise _Exhausted(deadline=True)


_local = threading.local()


def _tick() -> None:
    guard: _Guard | None = getattr(_local, "guard", None)
    if guard is not None:
        guard.tick()


if hasattr(_rrule, "_iterinfo"):  # dateutil 2.x: one `getdayset` call per iterator step

    class _GuardedIterInfo(_rrule._iterinfo):  # type: ignore[misc]
        """dateutil's per-iteration helper, counting the steps of the current thread's
        `_Guard` (no effect in a thread without one)."""

        def ydayset(self, *args: Any) -> Any:
            _tick()
            return super().ydayset(*args)

        def mdayset(self, *args: Any) -> Any:
            _tick()
            return super().mdayset(*args)

        def wdayset(self, *args: Any) -> Any:
            _tick()
            return super().wdayset(*args)

        def ddayset(self, *args: Any) -> Any:
            _tick()
            return super().ddayset(*args)

    _rrule._iterinfo = _GuardedIterInfo
    GUARDED = True
else:  # pragma: no cover (a dateutil without the helper: only the time budget holds)
    GUARDED = False


# --- parsing and expansion ----------------------------------------------------------------


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


def expand(
    data: bytes,
    calendar: str,
    window: Window,
    *,
    budget_s: float = BUDGET_S,
    clock: Callable[[], float] = time.monotonic,
) -> Expansion:
    """The events of the feed `data` (named `calendar`) that meet `window`, expanded within
    `budget_s` seconds of `clock`."""
    deadline = clock() + budget_s
    parsed = parse(data)
    result = Expansion()
    components = list(parsed.walk("VEVENT"))
    if len(components) > MAX_COMPONENTS:
        raise IcsError("too_many_events")
    result.skipped += _drop_unusable(parsed)
    components = list(parsed.walk("VEVENT"))
    _name_uidless(components)
    recurring = _recurring_uids(components)
    shifts = _future_shifts(components)
    result.warnings += sum(_unknown_tzid(component) for component in components)
    for component in components:
        with contextlib.suppress(Exception):  # an odd rule is left as it is
            _until_through_the_day(component)
    try:
        query = recurring_ical_events.of(parsed, skip_bad_series=True)
        lower = query.to_datetime(window.first_day - _MARGIN)
        upper = query.to_datetime(window.end_day + _MARGIN)
        all_series = list(query.series)
    except Exception:
        raise IcsError("parse_failed") from None
    feed = _Feed(calendar, window, recurring, shifts)
    found: dict[str, tuple[Event, int]] = {}
    cut: set[int] = set()
    guard = _Guard(deadline, clock)
    _local.guard = guard
    try:
        for index, series in enumerate(all_series):
            if clock() > deadline:
                result.truncated += len(all_series) - index
                break
            guard.left = MAX_RULE_STEPS
            try:
                events, outcome = _series_events(series, lower, upper, feed)
            except _Exhausted as exhausted:
                if exhausted.deadline:
                    result.truncated += len(all_series) - index
                    break
                result.skipped += 1  # cannot reach the window cheaply
                continue
            except _Skipped:
                result.skipped += 1
                continue
            result.skipped += outcome.skipped
            if outcome.cut:
                cut.add(index)
            for event in events:
                _keep_unique(found, event, index)
    finally:
        _local.guard = None
    ordered = sorted(found.values(), key=lambda pair: pair[0].sort_key(window))
    cut.update(index for _event, index in ordered[MAX_PER_FEED:])
    result.truncated += len(cut)
    result.events = [event for event, _index in ordered[:MAX_PER_FEED]]
    return result


@dataclass(frozen=True)
class _Feed:
    """What every occurrence of a feed needs to become an `Event`."""

    calendar: str
    window: Window
    recurring: set[str]
    shifts: dict[tuple[str, str], dt.timedelta]


class _Skipped(Exception):
    """A malformed series, skipped whole."""


@dataclass
class _SeriesOutcome:
    skipped: int = 0
    cut: bool = False


def _series_events(
    series: Any, lower: Any, upper: Any, feed: _Feed
) -> tuple[list[Event], _SeriesOutcome]:
    """The events of one series in the window (the earliest `MAX_PER_SERIES`)."""
    outcome = _SeriesOutcome()
    events: list[Event] = []
    occurrences = _occurrences(series, lower, upper)
    generated = 0
    while True:
        try:
            occurrence = next(occurrences)
        except StopIteration:
            break
        except _Exhausted:
            raise
        except Exception:  # a malformed series: skip it whole
            raise _Skipped from None
        generated += 1
        if generated > MAX_GENERATED:
            outcome.cut = True
            break
        try:
            event = _event(occurrence, feed)
        except Exception:  # a malformed instance
            outcome.skipped += 1
            continue
        if event is not None:
            events.append(event)
    if len(events) > MAX_PER_SERIES:
        events.sort(key=lambda e: e.sort_key(feed.window))
        del events[MAX_PER_SERIES:]
        outcome.cut = True
    return events, outcome


def _keep_unique(found: dict[str, tuple[Event, int]], event: Event, index: int) -> None:
    """Add `event` to `found` by id. The same id twice is the same event (twin UID-less
    events, a library quirk) when the events are equal; otherwise both are kept, the second
    under an id that adds its start (never happens with a valid feed)."""
    seen = found.get(event.id)
    if seen is None:
        found[event.id] = (event, index)
        return
    if seen[0] == event:
        return
    twin = replace(event, id=event_id(event.id, "at-" + instance_key(event.start)))
    if twin.id not in found:
        found[twin.id] = (twin, index)


def _occurrences(series: Any, lower: Any, upper: Any) -> Iterator[Any]:
    yield from itertools.islice(series.between(lower, upper), MAX_GENERATED + 1)


# --- preparing the components -------------------------------------------------------------


def _name_uidless(components: list[Component]) -> None:
    """Give each VEVENT without a UID one from a digest of its content (not of the
    properties rewritten on every export)."""
    for component in components:
        raw = component.get("UID")
        if raw is not None and str(raw).strip():
            continue
        digest = hashlib.sha256()
        for name, value in component.property_items(recursive=True, sorted=True):
            if name in _VOLATILE or name == "UID":
                continue
            digest.update(name.encode() + b"\0")
            encoded: Any = value if isinstance(value, bytes) else value.to_ical()  # type: ignore[attr-defined]
            digest.update(encoded if isinstance(encoded, bytes) else str(encoded).encode())
            digest.update(b"\n")
        component["UID"] = icalendar.vText(f"no-uid-{digest.hexdigest()[:16]}")


def _recurring_uids(components: list[Component]) -> set[str]:
    """The UIDs of recurring events: a rule, extra dates or an override."""
    return {
        str(component["UID"])
        for component in components
        if any(key in component for key in ("RRULE", "RDATE", "RECURRENCE-ID"))
    }


def _future_shifts(components: list[Component]) -> dict[tuple[str, str], dt.timedelta]:
    """For each `RANGE=THISANDFUTURE` override, keyed by UID and the `instance_key` of its
    RECURRENCE-ID: how far it moves the instances that follow (its DTSTART minus its
    RECURRENCE-ID, as the library computes it)."""
    shifts: dict[tuple[str, str], dt.timedelta] = {}
    for component in components:
        prop = component.get("RECURRENCE-ID")
        if prop is None or str(prop.params.get("RANGE", "")).upper() != "THISANDFUTURE":
            continue
        try:
            rid = component.decoded("RECURRENCE-ID")
            start = component.decoded("DTSTART")
            shifts[(str(component["UID"]), instance_key(rid))] = _comparable(start, rid)
        except Exception:  # noqa: S112 (a broken override: its instances keep their start)
            continue
    return shifts


def _comparable(start: dt.date, rid: dt.date) -> dt.timedelta:
    if isinstance(start, dt.datetime) != isinstance(rid, dt.datetime):
        raise TypeError("a date and a date-time")
    if (
        isinstance(start, dt.datetime)
        and isinstance(rid, dt.datetime)
        and (start.tzinfo is None) != (rid.tzinfo is None)
    ):
        start, rid = start.replace(tzinfo=None), rid.replace(tzinfo=None)
    return start - rid


def _unknown_tzid(component: Component) -> bool:
    """A time of `component` names a TZID that resolved to nothing (it reads as floating)."""
    for name in _TIME_PROPERTIES:
        prop = component.get(name)
        if prop is None or "TZID" not in getattr(prop, "params", {}):
            continue
        try:
            value = component.decoded(name)
        except Exception:  # noqa: S112 (a broken time is counted where it fails)
            continue
        if isinstance(value, dt.datetime) and value.tzinfo is None:
            return True
    return False


def _until_through_the_day(component: Component) -> None:
    """A `DATE` `UNTIL` on a series with a timed DTSTART: the end of that day in the series'
    zone (in UTC for a zoned series, floating for a floating one)."""
    rules = component.get("RRULE")
    if rules is None:
        return
    try:
        start = component.decoded("DTSTART")
    except Exception:
        return
    if not isinstance(start, dt.datetime):
        return
    for rule in rules if isinstance(rules, list) else [rules]:
        untils = rule.get("UNTIL") if hasattr(rule, "get") else None
        if not untils or len(untils) != 1:
            continue
        until = untils[0]
        if isinstance(until, dt.datetime) or not isinstance(until, dt.date):
            continue
        last = dt.datetime.combine(until, dt.time(23, 59, 59), start.tzinfo)
        rule["UNTIL"] = [last if last.tzinfo is None else last.astimezone(dt.UTC)]


def _drop_unusable(calendar: icalendar.Calendar) -> int:
    """Remove the VEVENTs with no readable DTSTART, or whose rule recurs every second or
    minute or cannot be read; how many. The helpers after it see only readable events."""
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


# --- one occurrence -----------------------------------------------------------------------


def _event(occurrence: Any, feed: _Feed) -> Event | None:
    """One occurrence as an `Event`, or `None` when it is cancelled or outside the window."""
    window = feed.window
    component = occurrence.as_component(False)
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
    uid, instance = _identity(component, feed)
    return Event(
        id=event_id(uid, instance),
        calendar=feed.calendar,
        title=clean_line(str(component.get("SUMMARY", "")), MAX_TITLE) or UNTITLED,
        start=when[0],
        end=when[1],
        all_day=all_day,
        location=clean_line(str(component.get("LOCATION", "")), MAX_LOCATION),
        description=clean_text(str(component.get("DESCRIPTION", "")), MAX_DESCRIPTION),
        status=_status(component),
        recurring=instance is not None,
    )


def _identity(component: icalendar.Event, feed: _Feed) -> tuple[str, str | None]:
    """The UID and, for an instance of a recurring event, the `instance_key` of its
    original start: its RECURRENCE-ID, or for an instance that a `RANGE=THISANDFUTURE`
    override moved (it carries that override's RECURRENCE-ID), its start moved back."""
    uid = str(component["UID"])
    if uid not in feed.recurring or "RECURRENCE-ID" not in component:
        return uid, None
    prop = component["RECURRENCE-ID"]
    rid = component.decoded("RECURRENCE-ID")
    if str(prop.params.get("RANGE", "")).upper() == "THISANDFUTURE":
        shift = feed.shifts.get((uid, instance_key(rid)))
        start = component.decoded("DTSTART")
        return uid, instance_key(start - shift if shift is not None else start)
    return uid, instance_key(rid)


def _status(component: icalendar.Event) -> Status:
    return "tentative" if str(component.get("STATUS", "")).upper() == "TENTATIVE" else "confirmed"
