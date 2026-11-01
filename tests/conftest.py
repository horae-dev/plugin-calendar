# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures: synthetic ICS feeds served by a fake fetcher (never the network), fake
secrets and a fixed clock."""

import datetime as dt
from pathlib import Path

import pytest
from horae_sdk import Plugin

from horae_calendar import build
from horae_calendar.events import Window
from horae_calendar.fetch import FetchError

HERE = Path(__file__).parent
ICS = HERE / "fixtures" / "ics"
TOKEN = "fake-feed-token-for-tests"  # noqa: S105 (synthetic)
"""Part of every fake feed URL: it must never show up in any output, log or error."""
NOW = dt.datetime(2026, 10, 20, 4, 30, tzinfo=dt.UTC)


def feed(name: str) -> bytes:
    return (ICS / f"{name}.ics").read_bytes()


def url(name: str, host: str = "calendar.google.com") -> str:
    return f"https://{host}/calendar/ical/{TOKEN}/{name}/basic.ics"


def window(start: str, days: int, timezone: str = "Europe/Paris") -> Window:
    return Window.build(start, days, timezone, NOW)


class FakeFetcher:
    """Answers each URL from `feeds`: the bytes, or a `FetchError` reason to raise."""

    def __init__(self, feeds: dict[str, bytes | str]) -> None:
        self.feeds = feeds
        self.fetched: list[str] = []

    async def fetch(self, url: str) -> bytes:
        self.fetched.append(url)
        answer = self.feeds.get(url, "http_404")
        if isinstance(answer, str):
            raise FetchError(answer)
        return answer


URLS = [
    url("work"),  # 1
    url("family", "calendar.proton.me"),  # 2
    url("hostile"),  # 3
    url("empty"),  # 4
    url("down"),  # 5
    f"https://ics.example.org/{TOKEN}/feed.ics",  # 6: a host to allow
    f"https://unlisted.example.org/{TOKEN}/feed.ics",  # 7: a host never allowed
    url("broken"),  # 8
]
"""The feed URLs in `CALENDAR_ICS_URLS`, a calendar's `index` is its place (1-based)."""
WORK, FAMILY, HOSTILE, EMPTY, DOWN, OTHER, UNLISTED, BROKEN = range(1, 9)
ENVIRON = {"CALENDAR_ICS_URLS": " ".join(URLS) + "\n"}
FEEDS: dict[str, bytes | str] = {
    url("work"): feed("weekly-exdate"),
    url("family", "calendar.proton.me"): feed("all-day"),
    url("hostile"): feed("hostile"),
    url("empty"): b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\nEND:VCALENDAR\r\n",
    url("down"): "timeout",
    f"https://ics.example.org/{TOKEN}/feed.ics": feed("overrides"),
    url("broken"): b"<html>not a calendar</html>",
}


@pytest.fixture
def fetcher() -> FakeFetcher:
    return FakeFetcher(dict(FEEDS))


@pytest.fixture
def plugin(fetcher: FakeFetcher) -> Plugin:
    return build(fetcher, environ=ENVIRON, clock=lambda: NOW)
