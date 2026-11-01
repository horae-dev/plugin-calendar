# SPDX-License-Identifier: Apache-2.0
"""The plugin: one source, `calendar.events`, the events of a window from ICS feeds.

The core's plugin host starts this module as an MCP server over stdio (`python -m
horae_calendar`). It is a **pure source**: it returns data and proposes no intent
(preparation tasks are the `calendar.prep_tasks` transform, #5).

**Trusted and untrusted.** The tool's parameters come from the pipeline (trusted): which
calendars, the window, the allowed hosts. The feeds' URLs are a **secret** (each carries an
access token): one declared secret, `CALENDAR_ICS_URLS`, holds them all, separated by
whitespace, and a calendar refers to its URL by its 1-based `index`. The host builds the
plugin's environment from the declared secrets only, and refuses to start the plugin when
one cannot be resolved, so the plugin reads exactly that variable, at call time, and never
logs or returns it (`horae_calendar.fetch`). Everything in a feed is untrusted
(`horae_calendar.events`).

**Failures.** Each calendar is fetched and expanded on its own (the expansion in a worker
thread, within a time budget). When every calendar works in full the result is
`complete`. A calendar that works but skipped or truncated something is returned with
`complete: false`. When one fails, the call raises `PluginError` naming the calendar and a
fixed reason (the core then marks the step `degraded` and skips `act` steps, so nothing is
written from partial data), unless the pipeline passes `partial: true`: then the working
calendars' events are returned with `complete: false` and the failures listed in
`calendars`. When every calendar fails, the call always raises.
"""

import asyncio
import datetime as dt
import logging
import os
import re
from collections.abc import Callable, Mapping
from typing import Any, Literal, NotRequired, TypedDict
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from horae_sdk import Call, Output, Permissions, Plugin, PluginError

from horae_calendar.events import Event, Window, WindowError
from horae_calendar.fetch import Fetcher, FetchError, HttpFetcher, check_url
from horae_calendar.ics import Expansion, IcsError, expand

NAME = "calendar"
"""The plugin's name, also the namespace of its refs (`calendar.events`) and event ids."""
VERSION = "0.1.0"
HORAE = ">=0,<0.2"
COMMAND = ["python3", "-m", "horae_calendar"]
"""`runtime.command` of `plugin.yaml`. When installing, pin an absolute interpreter that has
this package (the host's `PATH` is fixed and its working directory empty)."""
HOSTS = ("calendar.google.com", "calendar.proton.me")
"""The feed hosts declared in `plugin.yaml`: Google Calendar's secret addresses and Proton
Calendar's share links. Others are approved per instance (README, "Other hosts")."""
SECRET = "CALENDAR_ICS_URLS"  # noqa: S105 (a secret's name, not its value)
"""The declared secret: every feed URL, separated by whitespace."""
PERMISSIONS = Permissions(network=list(HOSTS), secrets=[SECRET])
"""Declare the least you need: every network host, secret, path and intent type."""

MAX_CALENDARS = 16
MAX_EVENTS = 2000
MAX_ALLOWED_HOSTS = 16
CALENDAR_NAME = re.compile(r"[^\x00-\x1f\x7f-\x9f]{1,64}")
HOST = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?")

_log = logging.getLogger("horae_calendar")


class CalendarSpec(TypedDict):
    """One calendar: a display `name` and the 1-based `index` of its URL in the secret."""

    name: str
    index: int
    type: NotRequired[Literal["ics"]]


def build(
    fetcher: Fetcher | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    clock: Callable[[], dt.datetime] | None = None,
) -> Plugin:
    """The plugin, fetching feeds with `fetcher` (default `HttpFetcher()`), reading the
    secret and `TZ` from `environ` (default `os.environ`, read at call time) and the date
    from `clock` (only when a call gives no `start`)."""
    client: Fetcher = fetcher if fetcher is not None else HttpFetcher()
    env: Mapping[str, str] = environ if environ is not None else os.environ
    now = clock if clock is not None else (lambda: dt.datetime.now(dt.UTC))
    plugin = Plugin(
        NAME,
        version=VERSION,
        permissions=PERMISSIONS,
        description="Calendar events from ICS feeds (Proton, Google), recurrences expanded.",
    )

    @plugin.source(f"{NAME}.events")
    async def events(
        call: Call,
        calendars: list[CalendarSpec] | None = None,
        start: str | None = None,
        days: int = 2,
        timezone: str | None = None,
        allowed_hosts: list[str] | None = None,
        partial: bool = False,
    ) -> Output:
        """The events of `days` whole days from `start` (default: today) in `timezone`
        (default: the instance's, `TZ`; pass `"{{date}}"` and `"{{timezone}}"` from the
        pipeline), from each calendar's ICS feed (default: every URL of the secret).
        `allowed_hosts` adds feed hosts to the declared ones (they must match what was
        approved in `horae.lock`); `partial` returns what the working calendars give when
        another fails, instead of raising."""
        specs = _calendars(calendars)
        hosts = frozenset(HOSTS) | _hosts(allowed_hosts)
        try:
            window = Window.build(start, days, timezone or _instance_zone(env), now())
        except WindowError as error:
            raise PluginError(str(error)) from None
        urls = _urls(env)
        if specs is None:
            specs = [{"name": f"calendar-{n}", "index": n} for n in range(1, len(urls) + 1)]
        for position, spec in enumerate(specs):
            if spec["index"] > len(urls):
                raise PluginError(
                    f"config_invalid: calendars[{position}].index is {spec['index']} but "
                    f"{SECRET} holds {len(urls)} URL(s)"
                )
        results = await asyncio.gather(
            *(_calendar(client, urls[spec["index"] - 1], hosts, spec, window) for spec in specs)
        )
        return Output(_merge(specs, results, window, partial=partial))

    return plugin


def _instance_zone(env: Mapping[str, str]) -> str:
    """The instance's time zone: `TZ`, which the host sets for the plugin (`UTC` when it is
    not an IANA name)."""
    zone = (env.get("TZ") or "").strip().removeprefix(":")
    try:
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError):
        return "UTC"
    return zone


def _urls(env: Mapping[str, str]) -> list[str]:
    """The feed URLs of the secret (never echoed: errors give counts only)."""
    urls = (env.get(SECRET) or "").split()
    if not urls:
        raise PluginError(f"secret_missing: {SECRET} is not set or empty")
    if len(urls) > MAX_CALENDARS:
        raise PluginError(f"config_invalid: {SECRET} holds more than {MAX_CALENDARS} URLs")
    return urls


def _calendars(value: object) -> list[CalendarSpec] | None:
    """The `calendars` parameter, checked (trusted configuration: errors name the field)."""
    if value is None:
        return None
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_CALENDARS:
        raise PluginError(f"config_invalid: calendars must list 1 to {MAX_CALENDARS} calendars")
    names: set[str] = set()
    indexes: set[int] = set()
    for position, spec in enumerate(value):
        name = spec.get("name") if isinstance(spec, Mapping) else None
        index = spec.get("index") if isinstance(spec, Mapping) else None
        where = f"config_invalid: calendars[{position}]"
        if not isinstance(name, str) or not CALENDAR_NAME.fullmatch(name) or not name.strip():
            raise PluginError(f"{where}.name must be 1 to 64 printable characters")
        if name in names:
            raise PluginError(f"{where}.name is used twice")
        if type(index) is not int or not 1 <= index <= MAX_CALENDARS:
            raise PluginError(f"{where}.index must be a whole number from 1 to {MAX_CALENDARS}")
        if index in indexes:
            raise PluginError(f"{where}.index is used twice")
        if spec.get("type", "ics") != "ics":
            raise PluginError(f"{where}.type must be ics")
        names.add(name)
        indexes.add(index)
    return list(value)


def _hosts(value: object) -> frozenset[str]:
    if value is None:
        return frozenset()
    if not isinstance(value, list) or len(value) > MAX_ALLOWED_HOSTS:
        raise PluginError(f"config_invalid: allowed_hosts must list at most {MAX_ALLOWED_HOSTS}")
    for host in value:
        if (
            not isinstance(host, str)
            or len(host) > 253
            or not HOST.fullmatch(host)
            or host == "localhost"
            or host.endswith(".localhost")
        ):
            raise PluginError(
                "config_invalid: allowed_hosts: each host is a lowercase DNS name (no IP, no port)"
            )
    return frozenset(value)


async def _calendar(
    client: Fetcher,
    url: str,
    hosts: frozenset[str],
    spec: CalendarSpec,
    window: Window,
) -> Expansion | str:
    """The calendar's events, or the reason it failed (a fixed code)."""
    try:
        data = await client.fetch(check_url(url, hosts))
        result = await asyncio.to_thread(expand, data, spec["name"], window)
    except (FetchError, IcsError) as error:
        return error.reason
    except Exception as error:
        # The type only: a message could hold the URL or the feed's text.
        _log.error("calendar %s: unexpected %s", spec["index"], type(error).__name__)
        return "internal_error"
    if not result.complete or result.warnings:
        _log.debug(
            "calendar %s: %d skipped, %d truncated, %d warnings",
            spec["index"],
            result.skipped,
            result.truncated,
            result.warnings,
        )
    return result


def _merge(
    specs: list[CalendarSpec],
    results: list[Expansion | str],
    window: Window,
    *,
    partial: bool,
) -> dict[str, Any]:
    summaries: list[dict[str, Any]] = []
    kept: list[Event] = []
    earlier: set[str] = set()  # ids from the calendars before: an invitation in two
    failures: list[str] = []
    complete = True
    for spec, result in zip(specs, results, strict=True):
        if isinstance(result, str):
            summaries.append({"name": spec["name"], "ok": False, "error": result})
            failures.append(f"{spec['name']}: {result}")
            continue
        ids = [event.id for event in result.events]
        if len(set(ids)) != len(ids):  # pragma: no cover (expand guarantees it)
            raise PluginError(f"calendar failed: {spec['name']}: internal_error")
        own = [event for event in result.events if event.id not in earlier]
        earlier.update(ids)
        kept.extend(own)
        complete = complete and result.complete
        summaries.append(
            {
                "name": spec["name"],
                "ok": True,
                "complete": result.complete,
                "events": len(own),
                "skipped": result.skipped,
                "truncated": result.truncated,
                "warnings": result.warnings,
            }
        )
    if failures and (not partial or len(failures) == len(specs)):
        raise PluginError("calendar failed: " + "; ".join(failures))
    ordered = sorted(kept, key=lambda e: e.sort_key(window))
    dropped = max(0, len(ordered) - MAX_EVENTS)
    return {
        "window": window.to_json(),
        "complete": complete and not failures and not dropped,
        "truncated": dropped,
        "calendars": summaries,
        "events": [event.to_json() for event in ordered[:MAX_EVENTS]],
    }


def manifest() -> dict[str, Any]:
    """The `plugin.yaml` document of this plugin (`python -m horae_calendar --manifest`)."""
    return build().manifest(command=COMMAND, horae=HORAE, license="Apache-2.0")
