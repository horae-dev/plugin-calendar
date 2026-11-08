# SPDX-License-Identifier: Apache-2.0
"""`calendar.events` against a real CalDAV server (Radicale), seeded with recurring events.

Skipped unless `CALDAV_TEST_URL` names the server (CI starts Radicale as a service container,
pinned by digest; locally: `docker run --rm -p 5232:5232 ghcr.io/kozea/radicale:3.5.7`).
The server is plain HTTP on 127.0.0.1, which the plugin refuses by design, so the test lets
this one client speak `http` and skips the host check; everything else is the real path:
the `REPORT` with its time range, the multistatus parsing, the merge and the expansion.
"""

import os
import uuid
from collections.abc import Collection
from typing import Any

import anyio
import httpx
import pytest
from horae_sdk.testing import call

import horae_calendar.plugin as plugin_module
from conftest import NOW
from horae_calendar import build
from horae_calendar.fetch import HttpFetcher

BASE = os.environ.get("CALDAV_TEST_URL", "").rstrip("/")
pytestmark = pytest.mark.skipif(not BASE, reason="CALDAV_TEST_URL is not set")

USER = "alice"
PASSWORD = "synthetic-test-pass"  # noqa: S105 (synthetic)

PARIS = """BEGIN:VTIMEZONE
TZID:Europe/Paris
BEGIN:DAYLIGHT
TZOFFSETFROM:+0100
TZOFFSETTO:+0200
TZNAME:CEST
DTSTART:19700329T020000
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU
END:DAYLIGHT
BEGIN:STANDARD
TZOFFSETFROM:+0200
TZOFFSETTO:+0100
TZNAME:CET
DTSTART:19701025T030000
RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU
END:STANDARD
END:VTIMEZONE
"""
STANDUP = """BEGIN:VEVENT
UID:standup@example.org
DTSTAMP:20260901T000000Z
DTSTART;TZID=Europe/Paris:20261005T093000
DTEND;TZID=Europe/Paris:20261005T094500
RRULE:FREQ=WEEKLY;BYDAY=MO,TH
EXDATE;TZID=Europe/Paris:20261019T093000
SUMMARY:Team standup
END:VEVENT
BEGIN:VEVENT
UID:standup@example.org
RECURRENCE-ID;TZID=Europe/Paris:20261022T093000
DTSTAMP:20260901T000000Z
DTSTART;TZID=Europe/Paris:20261022T140000
DTEND;TZID=Europe/Paris:20261022T141500
SUMMARY:Standup (moved)
END:VEVENT
"""
LUNCH = """BEGIN:VEVENT
UID:lunch@example.org
DTSTAMP:20260901T000000Z
DTSTART;VALUE=DATE:20261023
DTEND;VALUE=DATE:20261024
SUMMARY:Family lunch
END:VEVENT
"""
LONG_AGO = """BEGIN:VEVENT
UID:old@example.org
DTSTAMP:20200101T000000Z
DTSTART:20200101T100000Z
DTEND:20200101T110000Z
SUMMARY:Long ago
END:VEVENT
"""


def ics(*body: str) -> str:
    return (
        "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//Horae tests//EN\n"
        + "".join(body)
        + "END:VCALENDAR\n"
    ).replace("\n", "\r\n")


@pytest.fixture(scope="module")
def collection() -> str:
    """A fresh calendar collection on the server, seeded; its URL."""
    url = f"{BASE}/{USER}/{uuid.uuid4().hex}/"
    auth = httpx.BasicAuth(USER, PASSWORD)
    with httpx.Client(auth=auth, timeout=10) as http:
        made = http.request("MKCALENDAR", url)
        assert made.status_code == 201, made.status_code
        for name, text in {
            "standup": ics(PARIS, STANDUP),
            "lunch": ics(LUNCH),
            "old": ics(LONG_AGO),
        }.items():
            put = http.put(
                f"{url}{name}.ics", content=text, headers={"Content-Type": "text/calendar"}
            )
            assert put.status_code == 201, put.status_code
    return url


def run(url: str, password: str = PASSWORD, **params: Any) -> Any:
    secret = f"{url} {USER} {password}"
    plugin = build(
        HttpFetcher(schemes=("http",), timeout_s=10),
        environ={"CALENDAR_CALDAV": secret},
        clock=lambda: NOW,
    )

    def allow(url: str, hosts: Collection[str]) -> str:
        return url

    async def go() -> Any:
        return await call(plugin, "calendar.events", {"timezone": "Europe/Paris", **params})

    mp = pytest.MonkeyPatch()
    mp.setattr(plugin_module, "check_url", allow)
    try:
        return anyio.run(go)
    finally:
        mp.undo()


SPEC = {"name": "Radicale", "index": 1, "type": "caldav"}


def test_recurring_events_come_back_expanded(collection: str) -> None:
    replayed = run(collection, calendars=[SPEC], start="2026-10-19", days=7)
    assert replayed.error is None, replayed.error
    data = replayed.data
    assert data["complete"] is True
    assert data["calendars"][0]["ok"] is True
    got = [(e["title"], e["start"]) for e in data["events"]]
    assert got == [
        # Mon 19th is an EXDATE, Thu 22nd is moved to 14:00, Mon 26th is outside the window.
        ("Standup (moved)", "2026-10-22T14:00:00+02:00"),
        ("Family lunch", "2026-10-23"),
    ]
    assert next(e["recurring"] for e in data["events"]) is True


def test_two_weeks_expand_each_occurrence_in_the_right_zone(collection: str) -> None:
    data = run(collection, calendars=[SPEC], start="2026-10-26", days=7).data
    standups = [e["start"] for e in data["events"] if e["title"] == "Team standup"]
    # Europe/Paris falls back on Sunday 25 October: Monday 26th and Thursday 29th are CET.
    assert standups == ["2026-10-26T09:30:00+01:00", "2026-10-29T09:30:00+01:00"]


def test_an_event_outside_the_window_is_not_returned(collection: str) -> None:
    data = run(collection, calendars=[SPEC], start="2026-10-19", days=7).data
    assert "Long ago" not in [e["title"] for e in data["events"]]


def test_a_missing_collection_is_a_fixed_failure(collection: str) -> None:
    replayed = run(collection + "nope/", calendars=[SPEC])
    assert replayed.error is not None
    assert replayed.error.startswith("calendar failed: Radicale: http_")
    assert PASSWORD not in replayed.error


def test_a_wrong_password_is_http_401(collection: str) -> None:
    replayed = run(collection, "wrong-password", calendars=[SPEC])
    assert replayed.error == "calendar failed: Radicale: http_401"
