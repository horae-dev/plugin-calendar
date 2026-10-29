# SPDX-License-Identifier: Apache-2.0
"""The plugin: one source, `calendar.events`, the events of a window from ICS feeds.

The core's plugin host starts this module as an MCP server over stdio (`python -m
horae_calendar`). It is a **pure source**: it returns data and proposes no intent
(preparation tasks are the `calendar.prep_tasks` transform, #5).

**Trusted and untrusted.** The tool's parameters come from the pipeline (trusted): which
calendars, which secret holds each feed's URL, the window, extra hosts. Each feed's URL is a
**secret** (it carries the access token): read from the environment variable the host
injected for its declared secret, at call time, and never logged or returned
(`horae_calendar.fetch`). Everything in a feed is untrusted (`horae_calendar.events`).

**Failures.** Each calendar is fetched and expanded on its own. When every calendar works
the result is `complete`. When one fails, the call raises `PluginError` naming the calendar
and a fixed reason (the core then marks the step `degraded` and skips `act` steps, so
nothing is written from partial data), unless the pipeline passes `partial: true`: then the
working calendars' events are returned with `complete: false` and the failures listed in
`calendars`. When every calendar fails, the call always raises.
"""

import asyncio
import datetime as dt
import os
import re
from collections.abc import Callable, Mapping
from typing import Any, Literal, NotRequired, TypedDict

from horae_sdk import Call, Output, Permissions, Plugin, PluginError

from horae_calendar.events import Event, Window, WindowError
from horae_calendar.fetch import Fetcher, FetchError, HttpFetcher, check_url
from horae_calendar.ics import Expansion, IcsError, expand

NAME = "calendar"
"""The plugin's name, also the namespace of its refs (`calendar.events`) and event ids."""
VERSION = "0.1.0"
COMMAND = ["python3", "-m", "horae_calendar"]
"""`runtime.command` of `plugin.yaml`. When installing, pin an absolute interpreter that has
this package (the host's `PATH` is fixed and its working directory empty)."""
HOSTS = ("calendar.google.com", "calendar.proton.me")
"""The feed hosts declared in `plugin.yaml`: Google Calendar's secret addresses and Proton
Calendar's share links. Others are added per instance (README, "Other hosts")."""
SECRET = "CALENDAR_ICS_URL"  # noqa: S105 (a secret's name, not its value)
"""The declared secret of the first calendar. Others are named `CALENDAR_ICS_URL_<SUFFIX>`
and declared in the installed `plugin.yaml` (README, "More calendars")."""
PERMISSIONS = Permissions(network=list(HOSTS), secrets=[SECRET])
"""Declare the least you need: every network host, secret, path and intent type."""

MAX_CALENDARS = 16
MAX_EVENTS = 1000
MAX_EXTRA_HOSTS = 16
SECRET_NAME = re.compile(r"CALENDAR_ICS_URL(?:_[A-Z0-9]+)*")
CALENDAR_NAME = re.compile(r"[^\x00-\x1f\x7f-\x9f]{1,64}")
HOST = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?")


class CalendarSpec(TypedDict):
    """One calendar: a display `name` and the `secret` holding its feed URL."""

    name: str
    secret: str
    type: NotRequired[Literal["ics"]]


def build(
    fetcher: Fetcher | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    clock: Callable[[], dt.datetime] | None = None,
) -> Plugin:
    """The plugin, fetching feeds with `fetcher` (default `HttpFetcher()`), reading secrets
    from `environ` (default `os.environ`, read at call time) and the date from `clock` (only
    when a call gives no `start`)."""
    client: Fetcher = fetcher if fetcher is not None else HttpFetcher()
    secrets: Mapping[str, str] = environ if environ is not None else os.environ
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
        calendars: list[CalendarSpec],
        start: str | None = None,
        days: int = 2,
        timezone: str = "UTC",
        extra_hosts: list[str] | None = None,
        partial: bool = False,
    ) -> Output:
        """The events of `days` whole days from `start` (default: today) in `timezone`
        (pass `"{{date}}"` and `"{{timezone}}"` from the pipeline), from each calendar's ICS
        feed. `extra_hosts` adds feed hosts to the declared ones; `partial` returns what the
        working calendars give when another fails, instead of raising."""
        specs = _calendars(calendars)
        hosts = frozenset(HOSTS) | _hosts(extra_hosts)
        try:
            window = Window.build(start, days, timezone, now())
        except WindowError as error:
            raise PluginError(str(error)) from None
        results = await asyncio.gather(
            *(_calendar(client, secrets, hosts, spec, window) for spec in specs)
        )
        return Output(_merge(specs, results, window, partial=partial))

    return plugin


def _calendars(value: object) -> list[CalendarSpec]:
    """The `calendars` parameter, checked (trusted configuration: errors name the field)."""
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_CALENDARS:
        raise PluginError(f"calendars must list 1 to {MAX_CALENDARS} calendars")
    seen: set[str] = set()
    for index, spec in enumerate(value):
        name = spec.get("name") if isinstance(spec, Mapping) else None
        secret = spec.get("secret") if isinstance(spec, Mapping) else None
        if not isinstance(name, str) or not CALENDAR_NAME.fullmatch(name) or not name.strip():
            raise PluginError(f"calendars[{index}].name must be 1 to 64 printable characters")
        if name in seen:
            raise PluginError(f"calendars[{index}].name is used twice")
        seen.add(name)
        if not isinstance(secret, str) or not SECRET_NAME.fullmatch(secret):
            raise PluginError(
                f"calendars[{index}].secret must be {SECRET} or {SECRET}_<SUFFIX> (A-Z, 0-9)"
            )
        if spec.get("type", "ics") != "ics":
            raise PluginError(f"calendars[{index}].type must be ics")
    return list(value)


def _hosts(value: object) -> frozenset[str]:
    if value is None:
        return frozenset()
    if not isinstance(value, list) or len(value) > MAX_EXTRA_HOSTS:
        raise PluginError(f"extra_hosts must list at most {MAX_EXTRA_HOSTS} hosts")
    for host in value:
        if (
            not isinstance(host, str)
            or len(host) > 253
            or not HOST.fullmatch(host)
            or host == "localhost"
            or host.endswith(".localhost")
        ):
            raise PluginError("extra_hosts: each host is a lowercase DNS name (no IP, no port)")
    return frozenset(value)


async def _calendar(
    client: Fetcher,
    secrets: Mapping[str, str],
    hosts: frozenset[str],
    spec: CalendarSpec,
    window: Window,
) -> Expansion | str:
    """The calendar's events, or the reason it failed (a fixed code)."""
    url = (secrets.get(spec["secret"]) or "").strip()
    if not url:
        return "secret_missing"
    try:
        data = await client.fetch(check_url(url, hosts))
        return expand(data, spec["name"], window)
    except (FetchError, IcsError) as error:
        return error.reason
    except Exception:
        return "internal_error"


def _merge(
    specs: list[CalendarSpec],
    results: list[Expansion | str],
    window: Window,
    *,
    partial: bool,
) -> dict[str, Any]:
    summaries: list[dict[str, Any]] = []
    kept: dict[str, Event] = {}
    failures: list[str] = []
    for spec, result in zip(specs, results, strict=True):
        if isinstance(result, str):
            summaries.append({"name": spec["name"], "ok": False, "error": result})
            failures.append(f"{spec['name']}: {result}")
            continue
        count = 0
        for event in sorted(result.events, key=lambda e: e.sort_key(window)):
            if event.id not in kept:  # the same event in two calendars: the first one wins
                kept[event.id] = event
                count += 1
        summaries.append(
            {"name": spec["name"], "ok": True, "events": count, "skipped": result.skipped}
        )
    if failures and (not partial or len(failures) == len(specs)):
        raise PluginError("calendar failed: " + "; ".join(failures))
    if len(kept) > MAX_EVENTS:
        raise PluginError(f"more than {MAX_EVENTS} events in the window: ask for fewer days")
    ordered = sorted(kept.values(), key=lambda e: e.sort_key(window))
    return {
        "window": window.to_json(),
        "complete": not failures,
        "calendars": summaries,
        "events": [event.to_json() for event in ordered],
    }


def manifest() -> dict[str, Any]:
    """The `plugin.yaml` document of this plugin (`python -m horae_calendar --manifest`)."""
    return build().manifest(command=COMMAND, license="Apache-2.0")
