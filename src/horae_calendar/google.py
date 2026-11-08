# SPDX-License-Identifier: Apache-2.0
"""Google Calendar API v3: the events of a window, mapped to the common `Event` model.

`events.list` is called with `singleEvents=true` (Google expands the recurring events, so no
rule is evaluated here), `timeMin`/`timeMax` (the window), `orderBy=startTime` and a bounded
number of pages, for one calendar id.

**The credentials are a secret** (`CALENDAR_GOOGLE`, see `Credentials`): an OAuth client id,
its client secret and a long-lived refresh token, from the one-time consent that
`python -m horae_calendar google-auth` runs on the user's machine (never in the server).
Each call exchanges the refresh token at the token endpoint for a short-lived access token,
held in memory for that call only (`AccessTokens`) and shared by the account's calendars. A
401 from the API makes one fresh exchange and one retry. Nothing is written to disk, logged,
returned or put in an error: every failure is a `GoogleError` or `FetchError` with a fixed
`reason`, never Google's message.

**The answers are untrusted**: any field may be missing or of another type (a calendar's
events are written by whoever can invite the user). A bad item is counted in `skipped`.
"""

import asyncio
import datetime as dt
import json
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
from horae_calendar.fetch import Api
from horae_calendar.ics import Expansion

TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 (an endpoint, not a secret)
API_URL = "https://www.googleapis.com/calendar/v3/calendars"
SCOPE = "https://www.googleapis.com/auth/calendar.events.readonly"
"""Read-only access to events (`events.list` needs nothing more)."""
PAGE_SIZE = 250
MAX_PAGES = 10
MAX_EVENTS = 2000
"""Events kept per calendar (the earliest); more are counted in `truncated`."""
BUDGET_S = 30.0
"""Wall-clock time for one calendar, token exchange included; the pages not reached by then
are counted in `truncated`."""
CALENDAR_ID = re.compile(r"[^\x00-\x20\x7f-\x9f]{1,256}")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_FIELDS = (
    "nextPageToken,items(id,iCalUID,recurringEventId,originalStartTime,status,summary,"
    "location,description,start,end)"
)
_RATE_LIMITED = {
    "rateLimitExceeded",
    "userRateLimitExceeded",
    "quotaExceeded",
    "dailyLimitExceeded",
}
_AUTH_ERRORS = {"invalid_grant", "invalid_client", "unauthorized_client"}
UNTITLED = "(untitled)"


class GoogleError(Exception):
    """A calendar that cannot be read; `reason` is a fixed code (`auth_failed`,
    `rate_limited`, `bad_response`...), never Google's text."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Credentials:
    """One Google account of the `CALENDAR_GOOGLE` secret. Its `repr` hides everything."""

    client_id: str
    client_secret: str
    refresh_token: str

    def __repr__(self) -> str:
        return "Credentials(<hidden>)"


def accounts(secret: str, limit: int) -> list[Credentials] | None:
    """The accounts of the secret: whitespace-separated `client_id client_secret
    refresh_token` triples. `None` when it is not a whole number of triples, or has more than
    `limit` of them."""
    tokens = secret.split()
    if not tokens or len(tokens) % 3 or len(tokens) // 3 > limit:
        return None
    return [Credentials(*tokens[n : n + 3]) for n in range(0, len(tokens), 3)]


class AccessTokens:
    """The access token of one account for the length of one call: exchanged on first use,
    shared by the calendars that use it, replaced once when the API refuses it."""

    def __init__(self, api: Api, credentials: Credentials) -> None:
        self._api = api
        self._credentials = credentials
        self._token: str | None = None
        self._lock = asyncio.Lock()

    async def get(self, refused: str | None = None) -> str:
        """The token; a fresh one when `refused` (the token the API just rejected) is still
        the current one."""
        async with self._lock:
            if self._token is None or self._token == refused:
                self._token = await exchange(self._api, self._credentials)
            return self._token


async def exchange(api: Api, credentials: Credentials) -> str:
    """An access token for the refresh token (`GoogleError` `auth_failed` when Google refuses
    it: revoked, expired, or an app in "Testing" whose refresh tokens last 7 days)."""
    body = urlencode(
        {
            "client_id": credentials.client_id,
            "client_secret": credentials.client_secret,
            "refresh_token": credentials.refresh_token,
            "grant_type": "refresh_token",
        }
    ).encode("ascii")
    status, answer = await api.api(
        "POST",
        TOKEN_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        body=body,
    )
    data = _json(answer)
    if status == 200:
        token = data.get("access_token")
        if isinstance(token, str) and token and not any(c.isspace() for c in token):
            return token
        raise GoogleError("bad_response")
    error = data.get("error")
    if status in (400, 401) and error in _AUTH_ERRORS:
        raise GoogleError("auth_failed")
    raise GoogleError("rate_limited" if status == 429 else f"token_http_{status}")


def _json(raw: bytes) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def events_url(calendar_id: str, window: Window, page: str | None) -> str:
    """The `events.list` URL for the window (Google filters on overlap) and an optional page
    token."""
    params = {
        "singleEvents": "true",
        "orderBy": "startTime",
        "timeMin": _utc(window.start),
        "timeMax": _utc(window.end),
        "maxResults": str(PAGE_SIZE),
        "showDeleted": "false",
        "fields": _FIELDS,
    }
    if page:
        params["pageToken"] = page
    return f"{API_URL}/{quote(calendar_id, safe='')}/events?{urlencode(params)}"


def _utc(value: dt.datetime) -> str:
    return value.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


async def read(
    api: Api, tokens: AccessTokens, calendar_id: str, name: str, window: Window
) -> Expansion:
    """The calendar's events in the window (`GoogleError` or `FetchError` on failure)."""
    deadline = time.monotonic() + BUDGET_S
    result = Expansion()
    seen: set[str] = set()
    page: str | None = None
    token = await tokens.get()
    for number in range(MAX_PAGES):
        if number and time.monotonic() > deadline:
            result.truncated += 1
            return result
        token, data = await _page(api, tokens, token, events_url(calendar_id, window, page))
        items = data.get("items")
        if not isinstance(items, list):
            raise GoogleError("bad_response")
        for item in items:
            if len(result.events) >= MAX_EVENTS:
                result.truncated += 1
                return result
            event = to_event(item, name, window)
            if event is None:
                result.skipped += 1
            elif event is not _OUT and event.id not in seen:
                seen.add(event.id)
                result.events.append(event)
        following = data.get("nextPageToken")
        if not following:
            return result
        if not isinstance(following, str) or len(following) > 2048:
            raise GoogleError("bad_response")
        page = following
    result.truncated += 1  # still a next page after MAX_PAGES
    return result


async def _page(api: Api, tokens: AccessTokens, token: str, url: str) -> tuple[str, dict[str, Any]]:
    """One page, and the token that worked: after a 401, one fresh token and one retry."""
    for attempt in (1, 2):
        status, answer = await api.api("GET", url, headers={"Authorization": f"Bearer {token}"})
        data = _json(answer)
        if status == 200:
            return token, data
        if status == 401 and attempt == 1:
            token = await tokens.get(refused=token)
            continue
        raise GoogleError(_failure(status, data))
    raise GoogleError("auth_failed")  # pragma: no cover (the loop always returns or raises)


def _failure(status: int, data: dict[str, Any]) -> str:
    """The fixed code of a failed API answer."""
    if status == 401:
        return "auth_failed"
    if status == 429:
        return "rate_limited"
    if status == 403 and _reasons(data) & _RATE_LIMITED:
        return "rate_limited"
    return f"http_{status}"


def _reasons(data: dict[str, Any]) -> set[str]:
    error = data.get("error")
    errors = error.get("errors") if isinstance(error, dict) else None
    if not isinstance(errors, list):
        return set()
    return {e["reason"] for e in errors if isinstance(e, dict) and isinstance(e.get("reason"), str)}


_OUT = Event("", "", "", dt.date.min, dt.date.min, True)
"""Marks an item that is valid but not an event to return (cancelled, outside the window)."""


def to_event(item: object, calendar: str, window: Window) -> Event | None:
    """The `Event` of an `events.list` item; `None` when it cannot be read; `_OUT` when it
    is cancelled or does not meet the window."""
    if not isinstance(item, dict):
        return None
    if item.get("status") == "cancelled":
        return _OUT
    try:
        start = _when(item.get("start"), window)
        end = _when(item.get("end"), window)
        uid = _text(item.get("iCalUID")) or _text(item.get("id"))
        if start is None or uid is None:
            return None
        all_day = not isinstance(start, dt.datetime)
        if end is None:
            end = start if isinstance(start, dt.datetime) else start + dt.timedelta(days=1)
        if isinstance(start, dt.datetime) != isinstance(end, dt.datetime):
            return None
        if isinstance(start, dt.datetime) and isinstance(end, dt.datetime):
            meets = window.overlaps_timed(start, end)
        else:
            meets = window.overlaps_days(start, max(end, start + dt.timedelta(days=1)))
        if not meets:
            return _OUT
        instance = _instance(item, window)
    except (ValueError, OverflowError, ZoneInfoNotFoundError):
        return None
    status: Status = "tentative" if item.get("status") == "tentative" else "confirmed"
    return Event(
        id=event_id(uid, instance),
        calendar=calendar,
        title=clean_line(item.get("summary"), MAX_TITLE) or UNTITLED,
        start=start,
        end=end,
        all_day=all_day,
        location=clean_line(item.get("location"), MAX_LOCATION),
        description=clean_text(item.get("description"), MAX_DESCRIPTION),
        status=status,
        recurring=instance is not None,
    )


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= 1024 else None


def _instance(item: Mapping[str, Any], window: Window) -> str | None:
    """The instance key of an instance of a recurring event: its original start (so a moved
    instance keeps its id), as for an ICS `RECURRENCE-ID`."""
    if not item.get("recurringEventId"):
        return None
    original = _when(item.get("originalStartTime"), window)
    if original is not None:
        return instance_key(original)
    return _text(item.get("id"))


def _when(value: object, window: Window) -> dt.datetime | dt.date | None:
    """A Google `start`/`end` object: `{date}` (all day) or `{dateTime, timeZone?}`. A
    date-time carries its offset; one without is read in its `timeZone`, else the window's.
    The result is in the window's zone."""
    if not isinstance(value, dict):
        return None
    day = value.get("date")
    if isinstance(day, str):
        if not _DATE.fullmatch(day):
            raise ValueError("date")
        return dt.date.fromisoformat(day)
    moment = value.get("dateTime")
    if not isinstance(moment, str) or len(moment) > 40:
        return None
    parsed = dt.datetime.fromisoformat(moment)
    zone = value.get("timeZone")
    if parsed.tzinfo is None and isinstance(zone, str):
        parsed = parsed.replace(tzinfo=ZoneInfo(zone))
    return window.aware(parsed)
