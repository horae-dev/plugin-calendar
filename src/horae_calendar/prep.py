# SPDX-License-Identifier: Apache-2.0
"""The `calendar.prep_tasks` transform (#5): `calendar.events` data to `task.create` intents,
with no LLM, for events that need preparation.

It reads no clock, file or environment: `today` is a parameter (the pipeline passes
`{{date}}`), and everything else comes from the parameters and the input, so a run and its
replay propose the same tasks.

**Rules** (`rules`, trusted configuration, at most `MAX_RULES`). An event is matched against
the rules in order and the first one that matches decides; an event no rule matches gets no
task. A rule is an object with these keys, all optional but `keywords` or `regex`:

- `keywords`: words or phrases; the rule matches when the title contains one, ignoring case.
- `regex`: a Python regular expression searched in the title, case-insensitively. It is
  checked when the rules are read: at most `MAX_REGEX` characters, and a quantified group that
  itself holds a quantifier (`(a+)+`, the shape of catastrophic backtracking) is refused.
- `calendars`: calendar names; the rule only applies to events of those calendars.
- `lead_days`: how many days before the event the task is due (0 to `MAX_LEAD_DAYS`, default 1:
  the day before).
- `title`: the task title, `Prepare: ${title}` by default. `${title}` is replaced, once, by the
  event's title; the template is configuration, the title is untrusted, and the whole result
  goes through `intents.task_create`, which escapes it for Obsidian Tasks.

`keywords` and `regex` together match when either does.

**One task per event occurrence.** The fingerprint is `calendar:prep:<event id>` (the id
already tells the instances of a recurring event apart), so each occurrence has its own task
and a run that sees the same event again proposes the same fingerprint (the core dedupes).

**Dates.** The due date is the event's own calendar date, as its time zone writes it (the
offset in `start`), minus `lead_days` whole days: calendar arithmetic, so a daylight-saving
change or a month's end cannot move it by a day. An event that already ended before `today`
gets no task; a due date before `today` becomes `today`.

**Bounds.** At most `max_tasks` tasks (default `DEFAULT_MAX_TASKS`, at most `MAX_TASKS`),
earliest events first; the summary counts the events left out. At most `MAX_EVENTS` events are
read.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from horae_sdk import PluginError, intents

REF = "calendar.prep_tasks"
SOURCE = "calendar.events"

MAX_RULES = 20
MAX_KEYWORDS = 20
MAX_KEYWORD = 100
MAX_REGEX = 200
MAX_CALENDARS = 16
MAX_LEAD_DAYS = 60
DEFAULT_LEAD_DAYS = 1
DEFAULT_TITLE = "Prepare: ${title}"
MAX_TEMPLATE = 200
PLACEHOLDER = "${title}"
DEFAULT_MAX_TASKS = 50
MAX_TASKS = 200
MAX_EVENTS = 2000
_KNOWN = frozenset({"today", "rules", "max_tasks"})
_RULE_KEYS = frozenset({"keywords", "regex", "calendars", "lead_days", "title"})
_NESTED = re.compile(r"\([^()]*[+*{][^()]*\)\s*[+*{]")
_KEY = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_CONTROL = re.compile("[\\x00-\\x1f\\x7f-\\x9f\\u2028\\u2029]")


class PrepError(PluginError):
    """The parameters or the input are not what the transform expects (the call fails)."""


@dataclass(frozen=True)
class Rule:
    keywords: tuple[str, ...]
    regex: re.Pattern[str] | None
    calendars: frozenset[str] | None
    lead_days: int
    title: str

    def matches(self, title: str, calendar: str) -> bool:
        if self.calendars is not None and calendar not in self.calendars:
            return False
        folded = title.casefold()
        if any(word in folded for word in self.keywords):
            return True
        return self.regex is not None and self.regex.search(title) is not None


@dataclass(frozen=True)
class Params:
    today: date
    rules: tuple[Rule, ...]
    max_tasks: int


def _rule(raw: object, where: str) -> Rule:
    if not isinstance(raw, Mapping) or not set(raw) <= _RULE_KEYS:
        raise PrepError(f"{where} is an object with {', '.join(sorted(_RULE_KEYS))}")
    words = raw.get("keywords", [])
    if (
        not isinstance(words, list)
        or len(words) > MAX_KEYWORDS
        or not all(isinstance(w, str) and 1 <= len(w.strip()) <= MAX_KEYWORD for w in words)
    ):
        raise PrepError(f"{where}.keywords is up to {MAX_KEYWORDS} words of 1 to {MAX_KEYWORD}")
    pattern = raw.get("regex")
    regex: re.Pattern[str] | None = None
    if pattern is not None:
        if not isinstance(pattern, str) or not 1 <= len(pattern) <= MAX_REGEX:
            raise PrepError(f"{where}.regex is a pattern of 1 to {MAX_REGEX} characters")
        if _NESTED.search(pattern):
            raise PrepError(f"{where}.regex has a repeated group that repeats (too costly)")
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error:
            raise PrepError(f"{where}.regex is not a valid regular expression") from None
    if not words and regex is None:
        raise PrepError(f"{where} needs keywords or a regex")
    names = raw.get("calendars")
    calendars: frozenset[str] | None = None
    if names is not None:
        if (
            not isinstance(names, list)
            or not 1 <= len(names) <= MAX_CALENDARS
            or not all(isinstance(n, str) for n in names)
        ):
            raise PrepError(f"{where}.calendars lists 1 to {MAX_CALENDARS} names")
        calendars = frozenset(names)
    lead = raw.get("lead_days", DEFAULT_LEAD_DAYS)
    if type(lead) is not int or not 0 <= lead <= MAX_LEAD_DAYS:
        raise PrepError(f"{where}.lead_days is a whole number from 0 to {MAX_LEAD_DAYS}")
    template = raw.get("title", DEFAULT_TITLE)
    if (
        not isinstance(template, str)
        or not template.strip()
        or len(template) > MAX_TEMPLATE
        or _CONTROL.search(template)
    ):
        raise PrepError(f"{where}.title is one line of at most {MAX_TEMPLATE} characters")
    return Rule(
        keywords=tuple(w.strip().casefold() for w in words),
        regex=regex,
        calendars=calendars,
        lead_days=lead,
        title=template,
    )


def parse_params(params: Mapping[str, Any]) -> Params:
    """The transform's parameters; an unknown or invalid one is an error (never ignored)."""
    unknown = sorted(str(name) for name in set(params) - _KNOWN)
    if unknown:
        raise PrepError(f"unknown parameters: {', '.join(unknown)}")
    raw_today = params.get("today")
    try:
        today = date.fromisoformat(raw_today) if isinstance(raw_today, str) else None
    except ValueError:
        today = None
    if today is None or len(str(raw_today)) != 10:
        raise PrepError("today must be a date, YYYY-MM-DD (`{{date}}`)")
    raw = params.get("rules")
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_RULES:
        raise PrepError(f"rules must list 1 to {MAX_RULES} rules")
    max_tasks = params.get("max_tasks")
    if max_tasks is None:
        max_tasks = DEFAULT_MAX_TASKS
    if type(max_tasks) is not int or not 1 <= max_tasks <= MAX_TASKS:
        raise PrepError(f"max_tasks must be a whole number from 1 to {MAX_TASKS}")
    rules = tuple(_rule(rule, f"rules[{n}]") for n, rule in enumerate(raw))
    return Params(today=today, rules=rules, max_tasks=max_tasks)


@dataclass(frozen=True)
class _Event:
    id: str
    calendar: str
    title: str
    first: date
    last: date
    """The event's first day and last day, as its own time zone writes them."""


def _event(raw: object) -> _Event | None:
    """One `calendar.events` entry, or `None` when it is malformed."""
    if not isinstance(raw, Mapping):
        return None
    id_, calendar, title = raw.get("id"), raw.get("calendar"), raw.get("title")
    start, end = raw.get("start"), raw.get("end")
    if not (
        isinstance(id_, str)
        and id_
        and isinstance(calendar, str)
        and isinstance(title, str)
        and isinstance(start, str)
        and isinstance(end, str)
    ):
        return None
    try:
        if raw.get("all_day") is True:
            first = date.fromisoformat(start)
            last = date.fromisoformat(end) - timedelta(days=1)  # `end` is exclusive
        else:
            first = datetime.fromisoformat(start).date()  # the date in the event's own offset
            last = datetime.fromisoformat(end).date()
    except ValueError:
        return None
    return _Event(id_, calendar, title, first, max(first, last))


def _collect(input: Any) -> tuple[list[_Event], int]:
    """The well-formed, non-duplicate events of every `calendar.events` source in `input`,
    and how many entries were skipped."""
    if not isinstance(input, list):
        raise PrepError("the input is the collect step's list of sources")
    sources = [e for e in input if isinstance(e, Mapping) and e.get("source") == SOURCE]
    if not sources:
        raise PrepError(f"the input has no {SOURCE} data")
    events: dict[str, _Event] = {}
    skipped = 0
    for source in sources:
        data = source.get("data")
        if not isinstance(data, Mapping) or not isinstance(data.get("events"), list):
            raise PrepError(f"{SOURCE} data has no event list")
        for raw in data["events"][:MAX_EVENTS]:
            event = _event(raw)
            if event is None:
                skipped += 1
            else:
                events.setdefault(event.id, event)
    return sorted(events.values(), key=lambda e: (e.first, e.id)), skipped


def task_key(event_id: str) -> str:
    """`calendar:prep:<event id>` (the SDK's lossless hashed form when that is too long)."""
    key = f"calendar:prep:{event_id}"
    if _KEY.fullmatch(key):
        return key
    return intents.fingerprint("calendar", "prep", event_id)


def prep_tasks(
    params: Mapping[str, Any], input: Any
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """`(data, intents)`: a summary of what was decided, and the `task.create` proposals,
    earliest event first."""
    p = parse_params(params)
    events, skipped = _collect(input)
    proposals: list[dict[str, Any]] = []
    matched = past = left_out = 0
    for event in events:
        rule = next((r for r in p.rules if r.matches(event.title, event.calendar)), None)
        if rule is None:
            continue
        if event.last < p.today:
            past += 1
            continue
        matched += 1
        if len(proposals) >= p.max_tasks:
            left_out += 1
            continue
        due = max(event.first - timedelta(days=rule.lead_days), p.today)
        title = rule.title.replace(PLACEHOLDER, event.title, 1)
        proposals.append(intents.task_create(title, key=task_key(event.id), due=due))
    data = {
        "today": p.today.isoformat(),
        "events": len(events),
        "skipped": skipped,
        "matched": matched,
        "past": past,
        "tasks": len(proposals),
        "left_out": left_out,
    }
    return data, proposals


__all__ = ["REF", "SOURCE", "PrepError", "parse_params", "prep_tasks", "task_key"]
