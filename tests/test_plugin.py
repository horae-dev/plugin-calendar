# SPDX-License-Identifier: Apache-2.0
"""`calendar.events` through MCP, as the core's host calls it: merging, failures, parameter
checks, and the feed URL (a secret) never leaving the plugin."""

import json
import logging
from typing import Any

import anyio
import pytest
from horae_sdk import Plugin
from horae_sdk.testing import Replayed, call

import horae_calendar.plugin as plugin_module
from conftest import ENVIRON, FEEDS, NOW, TOKEN, FakeFetcher, url
from horae_calendar import build

TOOL = "calendar.events"
WORK = {"name": "Work", "secret": "CALENDAR_ICS_URL"}
FAMILY = {"name": "Family", "secret": "CALENDAR_ICS_URL_FAMILY"}


def run(plugin: Plugin, **params: Any) -> Replayed:
    async def go() -> Replayed:
        return await call(plugin, TOOL, params)

    return anyio.run(go)


def ok(plugin: Plugin, **params: Any) -> dict[str, Any]:
    replayed = run(plugin, **params)
    assert replayed.error is None, replayed.error
    assert replayed.intents == []  # a pure source
    assert isinstance(replayed.data, dict)
    return replayed.data


def error(plugin: Plugin, **params: Any) -> str:
    replayed = run(plugin, **params)
    assert replayed.error is not None, replayed.data
    assert TOKEN not in replayed.error
    return replayed.error


def test_two_calendars_merged_in_order(plugin: Plugin) -> None:
    data = ok(
        plugin,
        calendars=[WORK, FAMILY],
        start="2026-10-22",
        days=2,
        timezone="Europe/Paris",
    )
    assert data["complete"] is True
    assert data["window"] == {
        "start": "2026-10-22T00:00:00+02:00",
        "end": "2026-10-24T00:00:00+02:00",
        "timezone": "Europe/Paris",
        "days": 2,
    }
    assert data["calendars"] == [
        {"name": "Work", "ok": True, "events": 1, "skipped": 0},
        {"name": "Family", "ok": True, "events": 3, "skipped": 0},
    ]
    assert [(e["calendar"], e["title"], e["start"]) for e in data["events"]] == [
        ("Family", "Trip", "2026-10-18"),
        ("Work", "Team standup", "2026-10-22T09:30:00+02:00"),
        ("Family", "Birthday", "2026-10-23"),
        ("Family", "School holiday", "2026-10-23"),
    ]
    assert set(data["events"][1]) == {
        "id", "calendar", "title", "start", "end", "all_day", "location", "description",
        "status", "recurring",
    }  # fmt: skip


def test_default_window_is_today_for_two_days(plugin: Plugin) -> None:
    data = ok(plugin, calendars=[WORK])
    assert data["window"]["start"] == NOW.date().isoformat() + "T00:00:00+00:00"
    assert data["window"]["days"] == 2


def test_same_event_in_two_calendars_is_kept_once() -> None:
    environ = dict(ENVIRON, CALENDAR_ICS_URL_COPY=url("work"))
    plugin = build(FakeFetcher(dict(FEEDS)), environ=environ, clock=lambda: NOW)
    copy = {"name": "Copy", "secret": "CALENDAR_ICS_URL_COPY"}
    data = ok(plugin, calendars=[WORK, copy], start="2026-10-22", days=1)
    assert [e["calendar"] for e in data["events"]] == ["Work"]
    assert data["calendars"][1] == {"name": "Copy", "ok": True, "events": 0, "skipped": 0}


def test_empty_calendar_is_a_success(plugin: Plugin) -> None:
    data = ok(plugin, calendars=[{"name": "Empty", "secret": "CALENDAR_ICS_URL_EMPTY"}])
    assert data["events"] == []
    assert data["complete"] is True


DOWN = {"name": "Down", "secret": "CALENDAR_ICS_URL_DOWN"}


def test_one_failing_calendar_fails_the_call_by_default(plugin: Plugin) -> None:
    message = error(plugin, calendars=[WORK, DOWN], start="2026-10-22")
    assert message == "calendar failed: Down: timeout"


def test_partial_returns_the_working_calendars(plugin: Plugin) -> None:
    data = ok(plugin, calendars=[WORK, DOWN], start="2026-10-22", days=1, partial=True)
    assert data["complete"] is False
    assert data["calendars"] == [
        {"name": "Work", "ok": True, "events": 1, "skipped": 0},
        {"name": "Down", "ok": False, "error": "timeout"},
    ]
    assert [e["title"] for e in data["events"]] == ["Team standup"]


def test_every_calendar_failing_always_raises(plugin: Plugin) -> None:
    broken = {"name": "Broken", "secret": "CALENDAR_ICS_URL_BROKEN"}
    message = error(plugin, calendars=[DOWN, broken], partial=True)
    assert message == "calendar failed: Down: timeout; Broken: not_icalendar"


@pytest.mark.parametrize(
    ("secret", "reason"),
    [
        ("CALENDAR_ICS_URL_NOT_SET", "secret_missing"),
        ("CALENDAR_ICS_URL_UNLISTED", "host_not_allowed"),
        ("CALENDAR_ICS_URL_OTHER", "host_not_allowed"),
    ],
)
def test_calendar_failure_reasons(plugin: Plugin, secret: str, reason: str) -> None:
    message = error(plugin, calendars=[{"name": "X", "secret": secret}])
    assert message == f"calendar failed: X: {reason}"


def test_extra_hosts_allow_another_host(plugin: Plugin, fetcher: FakeFetcher) -> None:
    other = {"name": "Other", "secret": "CALENDAR_ICS_URL_OTHER"}
    data = ok(plugin, calendars=[other], start="2026-10-22", extra_hosts=["ics.example.org"])
    assert [e["title"] for e in data["events"]] == ["Daily sync (moved)"]  # 23 Oct moved out
    assert fetcher.fetched == [f"https://ics.example.org/{TOKEN}/feed.ics"]


def test_an_unexpected_error_is_reported_by_code_only() -> None:
    class Exploding:
        async def fetch(self, url: str) -> bytes:
            raise RuntimeError(f"boom while fetching {url}")

    plugin = build(Exploding(), environ=ENVIRON, clock=lambda: NOW)
    assert error(plugin, calendars=[WORK]) == "calendar failed: Work: internal_error"


def test_too_many_events(plugin: Plugin, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plugin_module, "MAX_EVENTS", 2)
    message = error(plugin, calendars=[WORK, FAMILY], start="2026-10-22")
    assert message == "more than 2 events in the window: ask for fewer days"


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"calendars": []}, "calendars must list 1 to 16 calendars"),
        ({"calendars": [WORK] * 17}, "calendars must list 1 to 16 calendars"),
        ({"calendars": [WORK, WORK]}, "calendars[1].name is used twice"),
        ({"calendars": [{"name": "", "secret": "CALENDAR_ICS_URL"}]}, "calendars[0].name"),
        ({"calendars": [{"name": "a\nb", "secret": "CALENDAR_ICS_URL"}]}, "calendars[0].name"),
        ({"calendars": [{"name": "W", "secret": "HOME"}]}, "calendars[0].secret must be"),
        ({"calendars": [{"name": "W", "secret": "GITHUB_TOKEN"}]}, "calendars[0].secret"),
        ({"calendars": [{"name": "W", "secret": "CALENDAR_ICS_URL_lower"}]}, "secret must"),
        ({"calendars": [WORK], "days": 0}, "days must be"),
        ({"calendars": [WORK], "timezone": "Nowhere/Land"}, "timezone is not"),
        ({"calendars": [WORK], "start": "tomorrow"}, "start must be"),
        ({"calendars": [WORK], "extra_hosts": ["127.0.0.1"]}, "extra_hosts"),
        ({"calendars": [WORK], "extra_hosts": ["localhost"]}, "extra_hosts"),
        ({"calendars": [WORK], "extra_hosts": ["a.localhost"]}, "extra_hosts"),
        ({"calendars": [WORK], "extra_hosts": ["ICS.example.org"]}, "extra_hosts"),
        ({"calendars": [WORK], "extra_hosts": ["ics.example.org:8443"]}, "extra_hosts"),
        ({"calendars": [WORK], "extra_hosts": ["h.example.org"] * 17}, "extra_hosts"),
    ],
)
def test_parameters_are_checked(plugin: Plugin, params: dict[str, Any], message: str) -> None:
    assert message in error(plugin, **params)


def test_unknown_calendar_type(plugin: Plugin) -> None:
    caldav = {"name": "W", "secret": "CALENDAR_ICS_URL", "type": "caldav"}
    assert "type" in error(plugin, calendars=[caldav])


def test_deterministic(plugin: Plugin) -> None:
    params: dict[str, Any] = {"calendars": [FAMILY, WORK], "start": "2026-10-15", "days": 14}
    assert ok(plugin, **params) == ok(plugin, **params)


def test_the_feed_url_never_leaves_the_plugin(
    plugin: Plugin, caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture[str]
) -> None:
    """The token in every URL is absent from data, errors, logs, stdout and stderr, for
    every outcome."""
    caplog.set_level(logging.DEBUG)
    seen: list[str] = []
    secrets = [name for name in ENVIRON if name != "CALENDAR_ICS_URL"]
    for secret in ["CALENDAR_ICS_URL", *secrets, "CALENDAR_ICS_URL_NOT_SET"]:
        spec = {"name": "X", "secret": secret}
        for partial in (False, True):
            replayed = run(plugin, calendars=[spec, WORK], start="2026-10-22", partial=partial)
            seen.append(json.dumps(replayed.expectation()))
    out, err = capfd.readouterr()
    for text in [*seen, caplog.text, out, err]:
        assert TOKEN not in text
    assert any('"ok": false' in text for text in seen)
