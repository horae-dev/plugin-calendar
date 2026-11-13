# SPDX-License-Identifier: Apache-2.0
"""The plugin: the source `calendar.events` (the events of a window from ICS feeds) and the
`calendar.prep_tasks` transform (preparation tasks from those events, `horae_calendar.prep`).

The core's plugin host starts this module as an MCP server over stdio (`python -m
horae_calendar`). The source is pure: it returns data and proposes no intent. Only the
`calendar.prep_tasks` transform proposes intents (`task.create`).

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
from collections.abc import Callable, Coroutine, Mapping
from typing import Any, Literal, NotRequired, TypedDict, cast
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from horae_sdk import Call, Output, Permissions, Plugin, PluginError

from horae_calendar import caldav, google, prep
from horae_calendar.events import Event, Window, WindowError
from horae_calendar.fetch import Api, Fetcher, FetchError, HttpFetcher, Reporter, check_url
from horae_calendar.ics import Expansion, IcsError, expand

NAME = "calendar"
"""The plugin's name, also the namespace of its refs (`calendar.events`) and event ids."""
VERSION = "0.1.2"
HORAE = ">=0,<0.2"
COMMAND = ["python3", "-m", "horae_calendar"]
"""`runtime.command` of `plugin.yaml`. When installing, pin an absolute interpreter that has
this package (the host's `PATH` is fixed and its working directory empty)."""
HOSTS = (
    "caldav.icloud.com",
    "calendar.google.com",
    "calendar.proton.me",
    "oauth2.googleapis.com",
    "www.googleapis.com",
)
"""The hosts declared in `plugin.yaml`: iCloud's CalDAV entry point, Google Calendar's secret
addresses, Proton Calendar's share links, and Google's token endpoint and Calendar API.
Others (an iCloud shard `pNN-caldav.icloud.com`, a Fastmail or Nextcloud server) are approved
per instance (README, "Other hosts")."""
SECRET = "CALENDAR_ICS_URLS"  # noqa: S105 (a secret's name, not its value)
"""The declared secret: every feed URL, separated by whitespace."""
CALDAV_SECRET = "CALENDAR_CALDAV"  # noqa: S105 (a secret's name, not its value)
"""The declared secret of the CalDAV calendars: `url user password` triples, separated by
whitespace (`horae_calendar.caldav.accounts`)."""
GOOGLE_SECRET = "CALENDAR_GOOGLE"  # noqa: S105 (a secret's name, not its value)
"""The declared secret of the Google accounts: `client_id client_secret refresh_token`
triples, separated by whitespace (`horae_calendar.google.accounts`)."""
PERMISSIONS = Permissions(
    network=list(HOSTS),
    optional_secrets=[SECRET, CALDAV_SECRET, GOOGLE_SECRET],
    intents=["task.create"],
)
"""Declare the least you need: every network host, secret, path and intent type. All three
secrets are optional (core decision 56): the host injects those that resolve, and a calendar
of a type whose secret is missing fails with `secret_missing`. The only
intent is `task.create`, which `calendar.prep_tasks` proposes (a task sink applies it)."""

MAX_CALENDARS = 16
MAX_EVENTS = 2000
MAX_ALLOWED_HOSTS = 16
CALENDAR_NAME = re.compile(r"[^\x00-\x1f\x7f-\x9f]{1,64}")
HOST = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?")

_log = logging.getLogger("horae_calendar")


class CalendarSpec(TypedDict):
    """One calendar: a display `name` and the 1-based `index` of its URL (or account) in the
    secret of its `type` (for Google, the account). A Google calendar also has a `calendar_id`
    (default `primary`)."""

    name: str
    index: int
    type: NotRequired[Literal["ics", "caldav", "google"]]
    calendar_id: NotRequired[str]


def build(
    fetcher: Fetcher | None = None,
    *,
    reporter: Reporter | None = None,
    api: Api | None = None,
    environ: Mapping[str, str] | None = None,
    clock: Callable[[], dt.datetime] | None = None,
) -> Plugin:
    """The plugin, fetching feeds with `fetcher` (default `HttpFetcher()`), reading the
    secret and `TZ` from `environ` (default `os.environ`, read at call time) and the date
    from `clock` (only when a call gives no `start`)."""
    client: Fetcher = fetcher if fetcher is not None else HttpFetcher()
    caldav_client: Reporter = (
        reporter
        if reporter is not None
        else client
        if isinstance(client, HttpFetcher)
        else HttpFetcher()
    )
    google_client: Api = (
        api if api is not None else client if isinstance(client, HttpFetcher) else HttpFetcher()
    )
    env: Mapping[str, str] = environ if environ is not None else os.environ
    now = clock if clock is not None else (lambda: dt.datetime.now(dt.UTC))
    plugin = Plugin(
        NAME,
        version=VERSION,
        permissions=PERMISSIONS,
        description="Calendar events from ICS feeds, CalDAV and Google, recurrences expanded.",
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
        approved in `horae.lock`, which the core also sends: a calendar whose host it does not
        approve is skipped, naming the host; an older core sends no list and only
        `allowed_hosts` applies); `partial` returns what the working calendars give when
        another fails, instead of raising."""
        specs = _calendars(calendars)
        hosts = frozenset(HOSTS) | _hosts(allowed_hosts)
        try:
            window = Window.build(start, days, timezone or _instance_zone(env), now())
        except WindowError as error:
            raise PluginError(str(error)) from None
        urls: list[str] = []
        if specs is None or any(spec.get("type", "ics") == "ics" for spec in specs):
            urls = _urls(env)
        if specs is None:
            specs = [{"name": f"calendar-{n}", "index": n} for n in range(1, len(urls) + 1)]
        accounts = _accounts(env) if any(spec.get("type") == "caldav" for spec in specs) else []
        google_accounts = (
            _google_accounts(env) if any(spec.get("type") == "google" for spec in specs) else []
        )
        for position, spec in enumerate(specs):
            kind = spec.get("type", "ics")
            counts = {"caldav": len(accounts), "google": len(google_accounts)}
            held = counts.get(kind, len(urls))
            secret = {"caldav": CALDAV_SECRET, "google": GOOGLE_SECRET}.get(kind, SECRET)
            noun = "URL" if kind == "ics" else "account"
            if spec["index"] > held:
                raise PluginError(
                    f"config_invalid: calendars[{position}].index is {spec['index']} but "
                    f"{secret} holds {held} {noun}(s)"
                )
        tokens = {  # one access token per account, for this call only
            n: google.AccessTokens(google_client, google_accounts[n - 1])
            for n in {spec["index"] for spec in specs if spec.get("type") == "google"}
        }

        allows = call.host_allowed

        def read(spec: CalendarSpec) -> Coroutine[Any, Any, Expansion | str]:
            kind = spec.get("type", "ics")
            if kind == "caldav":
                return _caldav(
                    caldav_client,
                    accounts[spec["index"] - 1],
                    hosts,
                    spec,
                    window,
                    call.host_allowed,
                )
            if kind == "google":
                return _google(google_client, tokens[spec["index"]], spec, window, allows)
            return _calendar(
                client, urls[spec["index"] - 1], hosts, spec, window, call.host_allowed
            )

        results = await asyncio.gather(*(read(spec) for spec in specs))
        return Output(_merge(specs, results, window, partial=partial))

    @plugin.transform(prep.REF)
    def prep_tasks(
        call: Call, today: Any = None, rules: Any = None, max_tasks: Any = None
    ) -> Output:
        """`task.create` proposals for the events that match a rule (`horae_calendar.prep`).
        No clock: `today` is the pipeline's `{{date}}`."""
        data, proposed = prep.prep_tasks(call.params, call.input)
        return Output(data, proposed)

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


def _accounts(env: Mapping[str, str]) -> list[caldav.Account]:
    """The CalDAV calendars of the secret (never echoed: errors give counts only)."""
    value = env.get(CALDAV_SECRET) or ""
    if not value.strip():
        raise PluginError(f"secret_missing: {CALDAV_SECRET} is not set or empty")
    found = caldav.accounts(value, MAX_CALENDARS)
    if found is None:
        raise PluginError(
            f"config_invalid: {CALDAV_SECRET} must hold at most {MAX_CALENDARS} "
            "`url user password` triples separated by whitespace"
        )
    return found


def _google_accounts(env: Mapping[str, str]) -> list[google.Credentials]:
    """The Google accounts of the secret (never echoed: errors give counts only)."""
    value = env.get(GOOGLE_SECRET) or ""
    if not value.strip():
        raise PluginError(f"secret_missing: {GOOGLE_SECRET} is not set or empty")
    found = google.accounts(value, MAX_CALENDARS)
    if found is None:
        raise PluginError(
            f"config_invalid: {GOOGLE_SECRET} must hold at most {MAX_CALENDARS} "
            "`client_id client_secret refresh_token` triples separated by whitespace"
        )
    return found


def _calendars(value: object) -> list[CalendarSpec] | None:
    """The `calendars` parameter, checked (trusted configuration: errors name the field)."""
    if value is None:
        return None
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_CALENDARS:
        raise PluginError(f"config_invalid: calendars must list 1 to {MAX_CALENDARS} calendars")
    names: set[str] = set()
    indexes: set[tuple[object, object, object]] = set()
    specs: list[CalendarSpec] = []
    for position, spec in enumerate(value):
        name = spec.get("name") if isinstance(spec, Mapping) else None
        index = spec.get("index") if isinstance(spec, Mapping) else None
        kind = spec.get("type", "ics") if isinstance(spec, Mapping) else None
        where = f"config_invalid: calendars[{position}]"
        if not isinstance(name, str) or not CALENDAR_NAME.fullmatch(name) or not name.strip():
            raise PluginError(f"{where}.name must be 1 to 64 printable characters")
        if name in names:
            raise PluginError(f"{where}.name is used twice")
        if type(index) is not int or not 1 <= index <= MAX_CALENDARS:
            raise PluginError(f"{where}.index must be a whole number from 1 to {MAX_CALENDARS}")
        if kind not in ("ics", "caldav", "google"):
            raise PluginError(f"{where}.type must be ics, caldav or google")
        calendar_id = spec.get("calendar_id") if isinstance(spec, Mapping) else None
        if kind == "google":
            calendar_id = "primary" if calendar_id is None else calendar_id
            if not isinstance(calendar_id, str) or not google.CALENDAR_ID.fullmatch(calendar_id):
                raise PluginError(f"{where}.calendar_id must be 1 to 256 characters, no spaces")
        elif calendar_id is not None:
            raise PluginError(f"{where}.calendar_id is for type google")
        key = (kind, index, calendar_id)
        if key in indexes:
            raise PluginError(f"{where}.index is used twice")
        names.add(name)
        indexes.add(key)
        checked: dict[str, Any] = {"name": name, "index": index, "type": kind}
        if kind == "google":
            checked["calendar_id"] = calendar_id
        specs.append(cast(CalendarSpec, checked))
    return specs


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


Allows = Callable[[str], bool | None]
"""`Call.host_allowed`: `True` for an `https` URL on a host approved in `horae.lock`, `False`
for any other, `None` when the core is older than horae-sdk 0.5 and sent no list."""


def _unapproved(allows: Allows, url: str) -> str | None:
    """The reason to skip a calendar whose URL the lock does not approve (it names the host,
    never the rest of the URL, which can hold a token); `None` when it is approved or the
    core sent no list (the declared and `allowed_hosts` check then stands, as before)."""
    if allows(url) is False:
        return f"host_not_approved: {urlsplit(url).hostname}"
    return None


async def _google(
    client: Api,
    tokens: google.AccessTokens,
    spec: CalendarSpec,
    window: Window,
    allows: Allows,
) -> Expansion | str:
    """The Google calendar's events, or the reason it failed (a fixed code)."""
    for endpoint in (google.TOKEN_URL, google.API_URL):  # the hosts this calendar contacts
        if (reason := _unapproved(allows, endpoint)) is not None:
            return reason
    try:
        return await google.read(
            client, tokens, spec.get("calendar_id", "primary"), spec["name"], window
        )
    except (FetchError, google.GoogleError) as error:
        return error.reason
    except Exception as error:
        # The type only: a message could hold a token.
        _log.error("calendar %s: unexpected %s", spec.get("index"), type(error).__name__)
        return "internal_error"


async def _caldav(
    client: Reporter,
    account: caldav.Account,
    hosts: frozenset[str],
    spec: CalendarSpec,
    window: Window,
    allows: Allows,
) -> Expansion | str:
    """The CalDAV calendar's events, or the reason it failed (a fixed code)."""
    try:
        url = check_url(account.url, hosts)
        if (reason := _unapproved(allows, url)) is not None:
            return reason
        shard = caldav.shard_follow(urlsplit(url).hostname or "", hosts)

        def follow(host: str) -> bool:
            # The credentials follow a redirect only to a host the lock approves too.
            return shard(host) and allows(f"https://{host}") is not False

        answer = await client.report(
            url,
            caldav.query(window),
            user=account.user,
            password=account.password,
            follow=follow,
        )
        result = await asyncio.to_thread(_expand_caldav, answer, spec["name"], window)
    except (FetchError, IcsError, caldav.CalDavError) as error:
        return error.reason
    except Exception as error:
        _log.error("calendar %s: unexpected %s", spec["index"], type(error).__name__)
        return "internal_error"
    return result


def _expand_caldav(answer: bytes, name: str, window: Window) -> Expansion:
    feed, unreadable = caldav.merge(caldav.parse_multistatus(answer))
    result = expand(feed, name, window)
    result.skipped += unreadable
    return result


async def _calendar(
    client: Fetcher,
    url: str,
    hosts: frozenset[str],
    spec: CalendarSpec,
    window: Window,
    allows: Allows,
) -> Expansion | str:
    """The calendar's events, or the reason it failed (a fixed code)."""
    try:
        checked = check_url(url, hosts)
        if (reason := _unapproved(allows, checked)) is not None:
            return reason
        data = await client.fetch(checked)
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
