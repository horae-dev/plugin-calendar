# SPDX-License-Identifier: Apache-2.0
"""`calendar.events` through MCP, as the core's host calls it: merging, failures, parameter
checks, and the feed URLs (a secret) never leaving the plugin."""

import json
import logging
from typing import Any

import anyio
import pytest
from horae_sdk import Plugin
from horae_sdk.testing import Replayed, call

import horae_calendar.plugin as plugin_module
from conftest import (
    BROKEN,
    DOWN,
    EMPTY,
    ENVIRON,
    FEEDS,
    NOW,
    OTHER,
    TOKEN,
    UNLISTED,
    URLS,
    FakeFetcher,
    url,
)
from horae_calendar import build

TOOL = "calendar.events"
WORK = {"name": "Work", "index": 1}
FAMILY = {"name": "Family", "index": 2}


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


def summary(name: str, events: int, **counts: Any) -> dict[str, Any]:
    base = {"name": name, "ok": True, "complete": True, "events": events}
    base.update({"skipped": 0, "truncated": 0, "warnings": 0})
    base.update(counts)
    return base


def plugin_with(urls: list[str], **environ: str) -> Plugin:
    env = {"CALENDAR_ICS_URLS": " ".join(urls), **environ}
    return build(FakeFetcher(dict(FEEDS)), environ=env, clock=lambda: NOW)


def test_two_calendars_merged_in_order(plugin: Plugin) -> None:
    data = ok(
        plugin,
        calendars=[WORK, FAMILY],
        start="2026-10-22",
        days=2,
        timezone="Europe/Paris",
    )
    assert data["complete"] is True
    assert data["truncated"] == 0
    assert data["window"] == {
        "start": "2026-10-22T00:00:00+02:00",
        "end": "2026-10-24T00:00:00+02:00",
        "timezone": "Europe/Paris",
        "days": 2,
    }
    assert data["calendars"] == [summary("Work", 1), summary("Family", 3)]
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


def test_default_calendars_are_every_url_of_the_secret() -> None:
    """PLAN.md's `calendar.events: { days: 2 }`: every URL, named `calendar-<n>`."""
    data = ok(plugin_with(URLS[:2]), days=2, start="2026-10-22")
    assert [c["name"] for c in data["calendars"]] == ["calendar-1", "calendar-2"]
    assert {e["calendar"] for e in data["events"]} == {"calendar-1", "calendar-2"}
    assert ok(plugin_with(URLS[:2]))["complete"] is True  # no parameter at all


def test_default_time_zone_is_the_instance_one() -> None:
    """The host sets `TZ` to the instance's time zone; `start` defaults to today there."""
    late = build(
        FakeFetcher(dict(FEEDS)),
        environ={"CALENDAR_ICS_URLS": URLS[0], "TZ": "Pacific/Auckland"},
        clock=lambda: NOW.replace(hour=20),
    )
    window = ok(late, calendars=[WORK])["window"]
    assert window["timezone"] == "Pacific/Auckland"
    assert window["start"] == "2026-10-21T00:00:00+13:00"  # already the 21st there


@pytest.mark.parametrize("tz", ["", "UTC0", ":/etc/localtime", "Nowhere/Land"])
def test_a_tz_that_is_not_an_iana_name_means_utc(tz: str) -> None:
    data = ok(plugin_with(URLS[:1], TZ=tz), calendars=[WORK])
    assert data["window"]["timezone"] == "UTC"


def test_tz_with_a_leading_colon() -> None:
    data = ok(plugin_with(URLS[:1], TZ=":Europe/Paris"), calendars=[WORK])
    assert data["window"]["timezone"] == "Europe/Paris"


def test_same_event_in_two_calendars_is_kept_once() -> None:
    plugin = plugin_with([url("work"), url("work")])
    copy = {"name": "Copy", "index": 2}
    data = ok(plugin, calendars=[WORK, copy], start="2026-10-22", days=1)
    assert [e["calendar"] for e in data["events"]] == ["Work"]
    assert data["calendars"][1] == summary("Copy", 0)


def test_empty_calendar_is_a_success(plugin: Plugin) -> None:
    data = ok(plugin, calendars=[{"name": "Empty", "index": EMPTY}])
    assert data["events"] == []
    assert data["complete"] is True


DOWN_SPEC = {"name": "Down", "index": DOWN}


def test_one_failing_calendar_fails_the_call_by_default(plugin: Plugin) -> None:
    message = error(plugin, calendars=[WORK, DOWN_SPEC], start="2026-10-22")
    assert message == "calendar failed: Down: timeout"


def test_partial_returns_the_working_calendars(plugin: Plugin) -> None:
    data = ok(plugin, calendars=[WORK, DOWN_SPEC], start="2026-10-22", days=1, partial=True)
    assert data["complete"] is False
    assert data["calendars"] == [
        summary("Work", 1),
        {"name": "Down", "ok": False, "error": "timeout"},
    ]
    assert [e["title"] for e in data["events"]] == ["Team standup"]


def test_every_calendar_failing_always_raises(plugin: Plugin) -> None:
    broken = {"name": "Broken", "index": BROKEN}
    message = error(plugin, calendars=[DOWN_SPEC, broken], partial=True)
    assert message == "calendar failed: Down: timeout; Broken: not_icalendar"


@pytest.mark.parametrize(
    ("index", "reason"), [(UNLISTED, "host_not_allowed"), (OTHER, "host_not_allowed")]
)
def test_calendar_failure_reasons(plugin: Plugin, index: int, reason: str) -> None:
    message = error(plugin, calendars=[{"name": "X", "index": index}])
    assert message == f"calendar failed: X: {reason}"


@pytest.mark.parametrize("value", [None, "", " \n"])
def test_a_missing_secret(value: str | None) -> None:
    environ = {} if value is None else {"CALENDAR_ICS_URLS": value}
    plugin = build(FakeFetcher(dict(FEEDS)), environ=environ, clock=lambda: NOW)
    assert (
        error(plugin, calendars=[WORK]) == "secret_missing: CALENDAR_ICS_URLS is not set or empty"
    )


def test_an_index_out_of_range_is_config_invalid() -> None:
    message = error(plugin_with(URLS[:2]), calendars=[{"name": "Third", "index": 3}])
    assert message == (
        "config_invalid: calendars[0].index is 3 but CALENDAR_ICS_URLS holds 2 URL(s)"
    )
    assert "example" not in message
    assert "calendar.google" not in message


def test_too_many_urls_in_the_secret() -> None:
    message = error(plugin_with(URLS * 3))
    assert message == "config_invalid: CALENDAR_ICS_URLS holds more than 16 URLs"


def test_urls_may_be_separated_by_any_whitespace() -> None:
    plugin = build(
        FakeFetcher(dict(FEEDS)),
        environ={"CALENDAR_ICS_URLS": f"  {URLS[0]}\t {URLS[1]}\n"},
        clock=lambda: NOW,
    )
    assert len(ok(plugin, start="2026-10-22")["calendars"]) == 2


def test_allowed_hosts_allow_another_host(plugin: Plugin, fetcher: FakeFetcher) -> None:
    other = {"name": "Other", "index": OTHER}
    data = ok(plugin, calendars=[other], start="2026-10-22", allowed_hosts=["ics.example.org"])
    assert [e["title"] for e in data["events"]] == ["Daily sync (moved)"]  # 23 Oct moved out
    assert fetcher.fetched == [f"https://ics.example.org/{TOKEN}/feed.ics"]


def test_an_unexpected_error_is_reported_by_code_and_logged_by_type(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Exploding:
        async def fetch(self, url: str) -> bytes:
            raise RuntimeError(f"boom while fetching {url}")

    caplog.set_level(logging.DEBUG)
    plugin = build(Exploding(), environ=ENVIRON, clock=lambda: NOW)
    assert error(plugin, calendars=[WORK]) == "calendar failed: Work: internal_error"
    assert "calendar 1: unexpected RuntimeError" in caplog.text
    assert TOKEN not in caplog.text
    assert "boom" not in caplog.text


def test_skip_counts_are_logged_at_debug(caplog: pytest.LogCaptureFixture) -> None:
    feeds = dict(FEEDS)
    feeds[url("work")] = (
        b"BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//t//EN\nBEGIN:VEVENT\nUID:s\n"
        b"DTSTAMP:20260101T000000Z\nDTSTART:20261022T000000Z\nRRULE:FREQ=SECONDLY\n"
        b"END:VEVENT\nEND:VCALENDAR\n"
    )
    caplog.set_level(logging.DEBUG, logger="horae_calendar")
    plugin = build(FakeFetcher(feeds), environ=ENVIRON, clock=lambda: NOW)
    data = ok(plugin, calendars=[WORK], start="2026-10-22")
    assert data["calendars"] == [summary("Work", 0, complete=False, skipped=1)]
    assert data["complete"] is False
    assert "calendar 1: 1 skipped, 0 truncated, 0 warnings" in caplog.text


def test_too_many_events_are_truncated(plugin: Plugin, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plugin_module, "MAX_EVENTS", 2)
    data = ok(plugin, calendars=[WORK, FAMILY], start="2026-10-22")
    assert data["complete"] is False
    assert data["truncated"] == 2
    assert [e["title"] for e in data["events"]] == ["Trip", "Team standup"]


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"calendars": []}, "calendars must list 1 to 16 calendars"),
        ({"calendars": [WORK] * 17}, "calendars must list 1 to 16 calendars"),
        ({"calendars": [WORK, WORK]}, "calendars[1].name is used twice"),
        ({"calendars": [WORK, {"name": "W2", "index": 1}]}, "calendars[1].index is used twice"),
        ({"calendars": [{"name": "", "index": 1}]}, "calendars[0].name"),
        ({"calendars": [{"name": "a\nb", "index": 1}]}, "calendars[0].name"),
        ({"calendars": [{"name": "W", "index": 0}]}, "calendars[0].index must be"),
        ({"calendars": [{"name": "W", "index": 17}]}, "calendars[0].index must be"),
        ({"calendars": [{"name": "W"}]}, "calendars.0.index"),
        ({"calendars": [WORK], "days": 0}, "days must be"),
        ({"calendars": [WORK], "timezone": "Nowhere/Land"}, "timezone is not"),
        ({"calendars": [WORK], "start": "tomorrow"}, "start must be"),
        ({"calendars": [WORK], "allowed_hosts": ["127.0.0.1"]}, "allowed_hosts"),
        ({"calendars": [WORK], "allowed_hosts": ["localhost"]}, "allowed_hosts"),
        ({"calendars": [WORK], "allowed_hosts": ["a.localhost"]}, "allowed_hosts"),
        ({"calendars": [WORK], "allowed_hosts": ["ICS.example.org"]}, "allowed_hosts"),
        ({"calendars": [WORK], "allowed_hosts": ["ics.example.org:8443"]}, "allowed_hosts"),
        ({"calendars": [WORK], "allowed_hosts": ["h.example.org"] * 17}, "allowed_hosts"),
    ],
)
def test_parameters_are_checked(plugin: Plugin, params: dict[str, Any], message: str) -> None:
    assert message in error(plugin, **params)


def test_unknown_calendar_type(plugin: Plugin) -> None:
    caldav = {"name": "W", "index": 1, "type": "caldav"}
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
    for index in [*range(1, len(URLS) + 1), len(URLS) + 1]:
        spec = {"name": "X", "index": index}
        for partial in (False, True):
            replayed = run(plugin, calendars=[spec, WORK], start="2026-10-22", partial=partial)
            seen.append(json.dumps(replayed.expectation()))
    out, err = capfd.readouterr()
    for text in [*seen, caplog.text, out, err]:
        assert TOKEN not in text
        assert "example.org/" not in text
    assert any('"ok": false' in text for text in seen)
