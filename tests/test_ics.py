# SPDX-License-Identifier: Apache-2.0
"""Recurrence expansion over a window, one synthetic fixture per case (issue #2)."""

import dataclasses
import datetime as dt
import time
from collections.abc import Iterator

import pytest

from conftest import feed, window
from horae_calendar import ics
from horae_calendar.events import Event
from horae_calendar.ics import MAX_PER_FEED, MAX_PER_SERIES, IcsError, expand


def times(name: str, start: str, days: int, tz: str = "Europe/Paris") -> list[tuple[str, ...]]:
    w = window(start, days, tz)
    events = sorted(expand(feed(name), "Cal", w).events, key=lambda e: e.sort_key(w))
    return [(e.to_json()["start"], e.to_json()["end"], e.title) for e in events]


def events(name: str, start: str, days: int, tz: str = "Europe/Paris") -> list[Event]:
    w = window(start, days, tz)
    return sorted(expand(feed(name), "Cal", w).events, key=lambda e: e.sort_key(w))


def test_weekly_with_exdate_and_dst() -> None:
    """MO,TH weekly; the Monday 19 Oct is an EXDATE; 26 Oct is after the switch to CET."""
    assert times("weekly-exdate", "2026-10-15", 14) == [
        ("2026-10-15T09:30:00+02:00", "2026-10-15T09:45:00+02:00", "Team standup"),
        ("2026-10-22T09:30:00+02:00", "2026-10-22T09:45:00+02:00", "Team standup"),
        ("2026-10-26T09:30:00+01:00", "2026-10-26T09:45:00+01:00", "Team standup"),
    ]


def test_monthly_by_day_across_time_zones() -> None:
    """2TU at 18:00 New York (EDT then EST: the US switch is 1 Nov) shown in Paris; -1FR in
    UTC with a DURATION."""
    assert times("monthly-byday", "2026-10-01", 45) == [
        ("2026-10-14T00:00:00+02:00", "2026-10-14T01:30:00+02:00", "Book club"),
        ("2026-10-30T16:00:00+01:00", "2026-10-30T17:00:00+01:00", "Monthly review"),
        ("2026-11-11T00:00:00+01:00", "2026-11-11T01:30:00+01:00", "Book club"),
    ]


def test_overrides_moved_retitled_out_and_in() -> None:
    assert times("overrides", "2026-10-21", 5) == [
        ("2026-10-21T10:00:00+02:00", "2026-10-21T10:30:00+02:00", "Daily sync"),
        ("2026-10-22T14:00:00+02:00", "2026-10-22T14:30:00+02:00", "Daily sync (moved)"),
        ("2026-10-24T10:00:00+02:00", "2026-10-24T10:30:00+02:00", "Daily sync"),
        ("2026-10-24T18:00:00+02:00", "2026-10-24T18:30:00+02:00", "Daily sync (moved in)"),
        ("2026-10-25T10:00:00+01:00", "2026-10-25T10:30:00+01:00", "Daily sync"),
    ]
    moved = events("overrides", "2026-10-21", 5)[1]
    assert moved.location == "Room 2"
    assert moved.recurring


def test_a_moved_instance_keeps_its_id() -> None:
    """The id comes from the RECURRENCE-ID (the original start), not the new start."""
    moved_in = events("overrides", "2026-10-21", 5)[3]
    original = events("overrides", "2026-10-29", 1)
    assert moved_in.title == "Daily sync (moved in)"
    assert all(e.id != moved_in.id for e in original)  # 29 Oct itself is gone (moved)
    other_window = [e for e in events("overrides", "2026-10-24", 1) if e.id == moved_in.id]
    assert other_window == [moved_in]


def test_cancelled_instances_and_series_are_left_out() -> None:
    assert times("cancelled", "2026-10-20", 3) == [
        ("2026-10-20T18:30:00+02:00", "2026-10-20T19:30:00+02:00", "Yoga"),
        ("2026-10-21T12:30:00+02:00", "2026-10-21T13:30:00+02:00", "Lunch"),
        ("2026-10-21T18:30:00+02:00", "2026-10-21T19:30:00+02:00", "Yoga"),
    ]
    lunch = events("cancelled", "2026-10-20", 3)[1]
    assert lunch.status == "tentative"
    assert not lunch.recurring


def test_all_day_events() -> None:
    """Exclusive end dates; a multi-day event started before the window; a yearly
    birthday with no DTEND (one day); one after the window."""
    found = events("all-day", "2026-10-22", 3)
    assert [(e.to_json()["start"], e.to_json()["end"], e.title) for e in found] == [
        ("2026-10-18", "2026-10-23", "Trip"),
        ("2026-10-23", "2026-10-24", "Birthday"),
        ("2026-10-23", "2026-10-24", "School holiday"),
    ]
    assert all(e.all_day for e in found)
    assert [e.recurring for e in found] == [False, True, False]


def test_all_day_events_ending_on_the_window_start_are_out() -> None:
    assert times("all-day", "2026-10-24", 1) == []


@pytest.mark.parametrize(
    ("tz", "offset"),
    [("Europe/Paris", "+02:00"), ("America/New_York", "-04:00"), ("UTC", "+00:00")],
)
def test_floating_times_are_read_in_the_window_time_zone(tz: str, offset: str) -> None:
    assert times("floating", "2026-10-23", 1, tz) == [
        (f"2026-10-23T08:00:00{offset}", f"2026-10-23T09:00:00{offset}", "Morning run"),
        (f"2026-10-23T21:30:00{offset}", f"2026-10-23T21:30:00{offset}", "Evening routine"),
    ]


def test_until_in_utc_local_and_date_and_count() -> None:
    found = times("until", "2026-10-19", 7)
    by_title: dict[str, list[str]] = {}
    for start, _end, title in found:
        by_title.setdefault(title, []).append(start)
    assert by_title == {
        # UNTIL=20261022T180000Z is 20:00 in Paris: the 22nd is included (inclusive bound)
        "Until in UTC": [f"2026-10-{d}T20:00:00+02:00" for d in (19, 20, 21, 22)],
        "Until in local time": [f"2026-10-{d}T07:00:00+02:00" for d in (19, 20, 21)],
        "Until a date": ["2026-10-19", "2026-10-20", "2026-10-21"],
        "Count of two": ["2026-10-20T12:00:00+02:00", "2026-10-21T12:00:00+02:00"],
    }


def test_custom_vtimezone_with_dst() -> None:
    """A non-IANA TZID defined by its VTIMEZONE (as Outlook writes it), across the switch."""
    assert times("custom-tzid", "2026-10-23", 8, "UTC") == [
        ("2026-10-23T07:00:00+00:00", "2026-10-23T08:00:00+00:00", "Planning"),
        ("2026-10-30T08:00:00+00:00", "2026-10-30T09:00:00+00:00", "Planning"),
    ]


def test_spring_forward_gap_uses_the_offset_before_it() -> None:
    """02:30 does not exist on 29 Mar 2026 in Paris: RFC 5545 reads it with +01:00."""
    assert times("dst", "2026-03-28", 3) == [
        ("2026-03-28T02:30:00+01:00", "2026-03-28T03:30:00+01:00", "Night shift"),
        ("2026-03-29T03:30:00+02:00", "2026-03-29T03:30:00+02:00", "Night shift"),
        ("2026-03-30T02:30:00+02:00", "2026-03-30T03:30:00+02:00", "Night shift"),
    ]


def test_fall_back_overlap_uses_the_first_occurrence() -> None:
    assert times("dst", "2026-10-24", 3) == [
        ("2026-10-24T02:30:00+02:00", "2026-10-24T02:45:00+02:00", "Backup"),
        ("2026-10-24T22:00:00+02:00", "2026-10-25T06:00:00+01:00", "Overnight"),
        ("2026-10-25T02:30:00+02:00", "2026-10-25T02:45:00+02:00", "Backup"),
        ("2026-10-26T02:30:00+01:00", "2026-10-26T02:45:00+01:00", "Backup"),
    ]


def test_events_overlapping_the_window_edges() -> None:
    """The overnight event starts the day before the window and ends in it."""
    assert [t[2] for t in times("dst", "2026-10-25", 1)] == ["Overnight", "Backup"]


def test_crlf_and_lf_give_the_same_events() -> None:
    w = window("2026-10-21", 5)
    lf = expand(feed("overrides"), "Cal", w).events
    crlf = expand(feed("overrides").replace(b"\n", b"\r\n"), "Cal", w).events
    assert lf == crlf


def test_ids_are_unique_and_stable() -> None:
    first = events("weekly-exdate", "2026-10-01", 31)
    again = events("weekly-exdate", "2026-10-01", 31)
    assert [e.id for e in first] == [e.id for e in again]
    assert len({e.id for e in first}) == len(first)
    assert all(e.id.startswith("calendar:standup-1-example.org:") for e in first)


def test_a_non_recurring_event_id_has_no_instance() -> None:
    from horae_sdk import intents

    (holiday,) = [e for e in events("all-day", "2026-10-23", 1) if e.title == "School holiday"]
    assert holiday.id == intents.fingerprint("calendar", "holiday-1@example.org")


def test_hostile_text_is_plain_and_capped() -> None:
    found = events("hostile", "2026-10-23", 1, "UTC")
    hostile, empty, no_title = found
    assert hostile.title.startswith("Pay 📅 2020-01-01 🔁 every day #tag [[Secret note]]")
    for text in (hostile.title, hostile.location or ""):
        assert not any(ch in text for ch in "\n\r\x07\x1b\u202e\u200b")
    assert not any(ch in (hostile.description or "") for ch in "\r\x07\x1b\u202e\u200b")
    assert hostile.location == "[click](https://evil.example.org) 📅 2020-01-01"
    assert hostile.description == "line one\nline two 🔺 [[x]] [31mred"
    assert empty.title == "(untitled)"
    assert no_title.title == "(untitled)"
    assert no_title.id.startswith("calendar:--uid-----2020-01-01::")


def test_an_event_without_uid_gets_a_content_id() -> None:
    data = feed("floating").replace(b"UID:run-1@example.org\n", b"")
    w = window("2026-10-23", 1)
    run = [e for e in expand(data, "Cal", w).events if e.title == "Morning run"]
    assert run[0].id.startswith("calendar:no-uid-")
    assert run == [e for e in expand(data, "Cal", w).events if e.title == "Morning run"]


@pytest.mark.parametrize(
    "data",
    [b"", b"<html>login</html>", b"BEGIN:VEVENT\nEND:VEVENT\n"],
)
def test_not_a_calendar(data: bytes) -> None:
    with pytest.raises(IcsError, match="not_icalendar"):
        expand(data, "Cal", window("2026-10-23", 1))


def test_unparseable_calendar() -> None:
    with pytest.raises(IcsError, match="parse_failed"):
        expand(b"BEGIN:VCALENDAR\nBEGIN:VEVENT\n", "Cal", window("2026-10-23", 1))


def test_bom_and_leading_blank_lines_are_accepted() -> None:
    data = b"\xef\xbb\xbf\r\n" + feed("floating")
    assert len(expand(data, "Cal", window("2026-10-23", 1)).events) == 2


def calendar(*vevents: str) -> bytes:
    body = "".join(f"BEGIN:VEVENT\nDTSTAMP:20260101T000000Z\n{v}\nEND:VEVENT\n" for v in vevents)
    return f"BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//t//EN\n{body}END:VCALENDAR\n".encode()


def test_malformed_events_are_skipped_not_fatal() -> None:
    data = calendar(
        "UID:good\nDTSTART:20261023T090000Z\nSUMMARY:Good",
        "UID:bad-rule\nDTSTART:20261023T090000Z\nRRULE:FREQ=SOMETIMES\nSUMMARY:Bad rule",
        "UID:bad-date\nDTSTART:2026-10-23\nSUMMARY:Bad date",
        "UID:no-start\nSUMMARY:No start",
    )
    result = expand(data, "Cal", window("2026-10-23", 1, "UTC"))
    assert [e.title for e in result.events] == ["Good"]
    assert result.skipped == 3
    assert not result.complete


def test_secondly_and_minutely_rules_are_skipped() -> None:
    data = calendar(
        "UID:s\nDTSTART:19700101T000000Z\nRRULE:FREQ=SECONDLY\nSUMMARY:Every second",
        "UID:m\nDTSTART:19700101T000000Z\nRRULE:FREQ=MINUTELY\nSUMMARY:Every minute",
        "UID:h\nDTSTART:20261022T000000Z\nRRULE:FREQ=HOURLY;COUNT=30\nSUMMARY:Hourly",
    )
    result = expand(data, "Cal", window("2026-10-23", 1, "UTC"))
    assert result.skipped == 2
    assert {e.title for e in result.events} == {"Hourly"}
    assert len(result.events) == 6  # 30 hours from 22 Oct 00:00: 23 Oct 00:00 to 05:00


def test_a_series_is_truncated_not_the_feed() -> None:
    """A half-hourly series over 62 days (2976 instances) keeps its earliest 500, and the
    other series of the feed are all there."""
    rule = "RRULE:FREQ=HOURLY;BYMINUTE=0,30"
    data = calendar(
        f"UID:h\nDTSTART:20261001T000000Z\n{rule}\nSUMMARY:Half-hourly",
        "UID:o\nDTSTART:20261130T120000Z\nSUMMARY:Once",
    )
    w = window("2026-10-01", 62, "UTC")
    result = expand(data, "Cal", w)
    half = [e for e in result.events if e.title == "Half-hourly"]
    assert len(half) == MAX_PER_SERIES
    assert half[0].to_json()["start"] == "2026-10-01T00:00:00+00:00"
    assert half[-1].to_json()["start"] == "2026-10-11T09:30:00+00:00"
    assert [e.title for e in result.events if e.title == "Once"] == ["Once"]
    assert (result.truncated, result.skipped, result.complete) == (1, 0, False)


def test_a_feed_is_truncated_at_its_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ics, "MAX_PER_FEED", 3)
    data = calendar(*(f"UID:e{n}\nDTSTART:2026102{n}T090000Z\nSUMMARY:E{n}" for n in range(1, 7)))
    result = expand(data, "Cal", window("2026-10-20", 10, "UTC"))
    assert [e.title for e in result.events] == ["E1", "E2", "E3"]  # the earliest
    assert result.truncated == 3
    assert not result.complete
    assert MAX_PER_FEED == 2000


def test_cancelled_instances_do_not_count_toward_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ics, "MAX_PER_SERIES", 3)
    data = calendar(
        "UID:d\nDTSTART:20261020T090000Z\nRRULE:FREQ=DAILY;COUNT=6\nSUMMARY:D",
        *(
            f"UID:d\nRECURRENCE-ID:2026102{n}T090000Z\nDTSTART:2026102{n}T090000Z\n"
            "STATUS:CANCELLED\nSUMMARY:D"
            for n in (0, 1, 2)
        ),
    )
    result = expand(data, "Cal", window("2026-10-20", 6, "UTC"))
    assert [e.start.day for e in result.events] == [23, 24, 25]
    assert result.complete


def test_margin_days_do_not_count_toward_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """The day before and after the window is expanded, but only what meets the window
    counts."""
    monkeypatch.setattr(ics, "MAX_PER_SERIES", 2)
    data = calendar("UID:d\nDTSTART:20261001T090000Z\nRRULE:FREQ=DAILY\nSUMMARY:D")
    result = expand(data, "Cal", window("2026-10-20", 2, "UTC"))
    assert len(result.events) == 2
    assert result.complete


def test_rules_that_never_reach_the_window_are_skipped_in_time() -> None:
    """A rule that never matches would make dateutil step to the year 9999: twenty of them
    are skipped (counted), well within the feed's time budget."""
    impossible = "RRULE:FREQ=DAILY;BYMONTH=2;BYMONTHDAY=30"
    data = calendar(
        *(f"UID:i{n}\nDTSTART:19710101T090000Z\n{impossible}\nSUMMARY:Never" for n in range(20)),
        "UID:ok\nDTSTART:20261023T090000Z\nSUMMARY:Real",
    )
    started = time.monotonic()
    result = expand(data, "Cal", window("2026-10-23", 1, "UTC"))
    assert time.monotonic() - started < ics.BUDGET_S
    assert [e.title for e in result.events] == ["Real"]
    assert result.skipped == 20
    assert result.truncated == 0
    assert not result.complete


def test_a_far_dtstart_within_the_step_budget_is_expanded() -> None:
    """A daily rule from 1980 (17,100 steps) and a weekly one from 1900 (6,600) are fine;
    a daily one from 1960 (24,400) is not."""
    data = calendar("UID:old\nDTSTART:19800101T090000Z\nRRULE:FREQ=DAILY\nSUMMARY:Old daily")
    data = calendar(
        "UID:old\nDTSTART:19800101T090000Z\nRRULE:FREQ=DAILY\nSUMMARY:Old daily",
        "UID:wk\nDTSTART:19000105T090000Z\nRRULE:FREQ=WEEKLY\nSUMMARY:Old weekly",
        "UID:too\nDTSTART:19600101T090000Z\nRRULE:FREQ=DAILY\nSUMMARY:Too old",
    )
    result = expand(data, "Cal", window("2026-10-23", 7, "UTC"))
    assert {e.title for e in result.events} == {"Old daily", "Old weekly"}
    assert result.skipped == 1


def test_the_time_budget_stops_the_feed() -> None:
    """Out of time: the series not yet expanded are counted in `truncated`."""
    ticks = iter(range(1000))
    data = calendar(*(f"UID:e{n}\nDTSTART:20261023T0{n}0000Z\nSUMMARY:E{n}" for n in range(5)))
    result = expand(
        data, "Cal", window("2026-10-23", 1, "UTC"), budget_s=2.5, clock=lambda: next(ticks)
    )
    # the clock gives 0 (start), then 1, 2 for two series, then 3 > 2.5: three left
    assert len(result.events) == 2
    assert result.truncated == 3
    assert not result.complete


def test_the_time_budget_stops_a_costly_rule() -> None:
    impossible = "RRULE:FREQ=DAILY;BYMONTH=2;BYMONTHDAY=30"
    data = calendar(f"UID:i\nDTSTART:19710101T090000Z\n{impossible}\nSUMMARY:Never")
    result = expand(data, "Cal", window("2026-10-23", 1, "UTC"), budget_s=0.0)
    assert result.events == []
    assert result.truncated == 1


def test_the_rule_guard_is_installed() -> None:
    assert ics.GUARDED


def test_this_and_future_instances_keep_their_own_ids() -> None:
    """The reviewer's case: every instance after a THISANDFUTURE override has an id from
    its own original start, and all of them are returned."""
    data = calendar(
        "UID:t\nDTSTART:20261020T090000Z\nDTEND:20261020T100000Z\nRRULE:FREQ=DAILY;COUNT=40\n"
        "SUMMARY:Old",
        "UID:t\nRECURRENCE-ID;RANGE=THISANDFUTURE:20261029T090000Z\nDTSTART:20261029T100000Z\n"
        "DTEND:20261029T110000Z\nSUMMARY:New",
    )
    w = window("2026-10-26", 7, "UTC")
    found = sorted(expand(data, "Cal", w).events, key=lambda e: e.sort_key(w))
    assert [e.start.day for e in found] == [26, 27, 28, 29, 30, 31, 1]
    assert len({e.id for e in found}) == 7
    assert [e.id.split(":")[2] for e in found[3:]] == [
        "20261029T090000Z", "20261030T090000Z", "20261031T090000Z", "20261101T090000Z",
    ]  # fmt: skip
    assert [e.title for e in found] == ["Old"] * 3 + ["New"] * 4


def test_this_and_future_through_the_tool_returns_every_day() -> None:
    import anyio
    from horae_sdk.testing import call

    from conftest import NOW, FakeFetcher, url
    from horae_calendar import build

    data = (
        b"BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//t//EN\nBEGIN:VEVENT\nUID:t\n"
        b"DTSTAMP:20260101T000000Z\nDTSTART;TZID=Europe/Paris:20261020T090000\n"
        b"RRULE:FREQ=DAILY;COUNT=40\nSUMMARY:Daily\nEND:VEVENT\nBEGIN:VEVENT\nUID:t\n"
        b"DTSTAMP:20260101T000000Z\n"
        b"RECURRENCE-ID;RANGE=THISANDFUTURE;TZID=Europe/Paris:20261029T090000\n"
        b"DTSTART;TZID=Europe/Paris:20261029T100000\nSUMMARY:Later\nEND:VEVENT\n"
        b"END:VCALENDAR\n"
    )
    plugin = build(
        FakeFetcher({url("t"): data}), environ={"CALENDAR_ICS_URLS": url("t")}, clock=lambda: NOW
    )
    params = {"start": "2026-10-26", "days": 7, "timezone": "Europe/Paris"}
    replayed = anyio.run(call, plugin, "calendar.events", params)
    assert replayed.error is None
    starts = [e["start"][:10] for e in replayed.data["events"]]
    assert starts == [f"2026-10-{d}" for d in (26, 27, 28, 29, 30, 31)] + ["2026-11-01"]
    assert len({e["id"] for e in replayed.data["events"]}) == 7


def test_unknown_tzid_is_floating_with_a_warning() -> None:
    result = expand(feed("unknown-tzid"), "Cal", window("2026-10-26", 1))
    assert [(e.title, e.to_json()["start"]) for e in result.events] == [
        ("Unknown zone", "2026-10-26T09:00:00+01:00"),
        ("Known zone", "2026-10-26T10:00:00+01:00"),
    ]
    assert result.warnings == 1
    assert result.complete


def test_a_date_until_on_a_timed_series_includes_that_day() -> None:
    """UNTIL=20261028 on a 23:30 Paris series: the 28th at 23:30 is in (it is 22:30 UTC,
    after midnight UTC, which the library would have used)."""
    data = calendar(
        "UID:u\nDTSTART;TZID=Europe/Paris:20261026T233000\nRRULE:FREQ=DAILY;UNTIL=20261028\n"
        "SUMMARY:Late",
        "UID:f\nDTSTART:20261026T233000\nRRULE:FREQ=DAILY;UNTIL=20261027\nSUMMARY:Floating",
    )
    found = expand(data, "Cal", window("2026-10-26", 5)).events
    late = sorted(e.to_json()["start"] for e in found if e.title == "Late")
    assert late == [f"2026-10-{d}T23:30:00+01:00" for d in (26, 27, 28)]
    assert sorted(e.start.day for e in found if e.title == "Floating") == [26, 27]


def test_an_event_ending_at_the_window_start_is_out() -> None:
    data = calendar(
        "UID:e\nDTSTART;TZID=Europe/Paris:20261022T230000\nDTEND;TZID=Europe/Paris:20261023T000000\n"
        "SUMMARY:Ends at midnight",
        "UID:s\nDTSTART;TZID=Europe/Paris:20261022T230000\nDTEND;TZID=Europe/Paris:20261023T000001\n"
        "SUMMARY:Ends a second later",
    )
    found = expand(data, "Cal", window("2026-10-23", 1)).events
    assert [e.title for e in found] == ["Ends a second later"]


def test_a_uidless_series_is_recurring_with_stable_ids() -> None:
    data = calendar("DTSTART:20261023T090000Z\nRRULE:FREQ=DAILY;COUNT=3\nSUMMARY:No UID")
    w = window("2026-10-23", 3, "UTC")
    found = expand(data, "Cal", w).events
    assert len(found) == 3
    assert all(e.recurring for e in found)
    uids = {e.id.split(":")[1] for e in found}
    assert len(uids) == 1
    assert next(iter(uids)).startswith("no-uid-")
    assert len({e.id for e in found}) == 3
    # a re-export (new DTSTAMP, SEQUENCE, LAST-MODIFIED) keeps the ids
    again = data.replace(b"DTSTAMP:20260101T000000Z", b"DTSTAMP:20261101T000000Z\nSEQUENCE:4")
    assert {e.id for e in expand(again, "Cal", w).events} == {e.id for e in found}


def test_uidless_twins_are_one_event() -> None:
    twin = "DTSTART:20261023T090000Z\nSUMMARY:Twin"
    found = expand(calendar(twin, twin), "Cal", window("2026-10-23", 1, "UTC")).events
    assert [e.title for e in found] == ["Twin"]
    assert not found[0].recurring


def test_a_uidless_event_does_not_make_none_recurring() -> None:
    data = calendar(
        "DTSTART:20261023T090000Z\nRRULE:FREQ=DAILY;COUNT=2\nSUMMARY:Rule",
        "UID:None\nDTSTART:20261023T100000Z\nSUMMARY:Called None",
    )
    found = expand(data, "Cal", window("2026-10-23", 1, "UTC")).events
    (named,) = [e for e in found if e.title == "Called None"]
    assert not named.recurring


def test_a_broken_instance_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    real = ics._event

    def flaky(occurrence: object, feed: object) -> object:
        event = real(occurrence, feed)  # type: ignore[arg-type]
        if event is not None and event.start.day == 24:
            raise ValueError("broken")
        return event

    monkeypatch.setattr(ics, "_event", flaky)
    data = calendar("UID:d\nDTSTART:20261023T090000Z\nRRULE:FREQ=DAILY;COUNT=3\nSUMMARY:D")
    result = expand(data, "Cal", window("2026-10-23", 3, "UTC"))
    assert [e.start.day for e in result.events] == [23, 25]
    assert result.skipped == 1
    assert not result.complete


def test_a_series_failing_while_expanding_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(series: object, lower: object, upper: object) -> Iterator[object]:
        if getattr(series, "uid", "") == "bad":
            raise ValueError("broken")
        yield from real(series, lower, upper)

    real = ics._occurrences
    monkeypatch.setattr(ics, "_occurrences", broken)
    data = calendar(
        "UID:bad\nDTSTART:20261023T090000Z\nSUMMARY:Bad",
        "UID:good\nDTSTART:20261023T100000Z\nSUMMARY:Good",
    )
    result = expand(data, "Cal", window("2026-10-23", 1, "UTC"))
    assert [e.title for e in result.events] == ["Good"]
    assert result.skipped == 1


def test_a_series_generating_too_much_is_cut(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ics, "MAX_GENERATED", 5)
    data = calendar("UID:d\nDTSTART:20261020T090000Z\nRRULE:FREQ=DAILY\nSUMMARY:D")
    result = expand(data, "Cal", window("2026-10-21", 10, "UTC"))
    assert len(result.events) == 4  # the margin day before the window was generated too
    assert result.truncated == 1


def test_rdate_adds_instances() -> None:
    data = calendar(
        "UID:r\nDTSTART:20261023T090000Z\nRDATE:20261024T150000Z,20261030T090000Z\nSUMMARY:R"
    )
    found = expand(data, "Cal", window("2026-10-23", 2, "UTC")).events
    assert sorted(e.to_json()["start"] for e in found) == [
        "2026-10-23T09:00:00+00:00",
        "2026-10-24T15:00:00+00:00",
    ]
    assert all(e.recurring for e in found)


def test_this_and_future_override() -> None:
    data = calendar(
        "UID:t\nDTSTART:20261020T090000Z\nDTEND:20261020T100000Z\nRRULE:FREQ=DAILY;COUNT=6\n"
        "SUMMARY:Old",
        "UID:t\nRECURRENCE-ID;RANGE=THISANDFUTURE:20261023T090000Z\n"
        "DTSTART:20261023T110000Z\nDTEND:20261023T120000Z\nSUMMARY:New",
    )
    found = expand(data, "Cal", window("2026-10-22", 3, "UTC")).events
    assert sorted((e.to_json()["start"], e.title) for e in found) == [
        ("2026-10-22T09:00:00+00:00", "Old"),
        ("2026-10-23T11:00:00+00:00", "New"),
        ("2026-10-24T11:00:00+00:00", "New"),
    ]


def test_dtend_before_dtstart_is_not_fatal() -> None:
    data = calendar("UID:x\nDTSTART:20261023T090000Z\nDTEND:20261023T080000Z\nSUMMARY:Odd")
    found = expand(data, "Cal", window("2026-10-23", 1, "UTC")).events
    # recurring-ical-events reads the inverted period as 08:00-09:00; never an error
    assert [(e.start.hour, e.end.hour) for e in found] == [(8, 9)]  # type: ignore[union-attr]


def test_x_wr_timezone_applies_to_floating_times() -> None:
    """Google's calendar-wide X-WR-TIMEZONE: floating times are read in it, UTC ones keep
    their instant."""
    data = (
        b"BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//t//EN\nX-WR-TIMEZONE:America/New_York\n"
        + calendar(
            "UID:f\nDTSTART:20261023T090000\nSUMMARY:Floating",
            "UID:u\nDTSTART:20261019T130000Z\nRRULE:FREQ=DAILY\nSUMMARY:UTC",
        ).split(b"PRODID:-//t//EN\n", 1)[1]
    )
    found = expand(data, "Cal", window("2026-10-23", 1)).events
    assert sorted((e.title, e.to_json()["start"]) for e in found) == [
        ("Floating", "2026-10-23T15:00:00+02:00"),
        ("UTC", "2026-10-23T15:00:00+02:00"),
    ]


def test_ids_stay_unique_within_a_feed() -> None:
    """Equal events under one id are one; a different event under a taken id gets an id
    with its start added (a library quirk, never a valid feed)."""
    w = window("2026-10-23", 1, "UTC")
    first = expand(calendar("UID:a\nDTSTART:20261023T090000Z\nSUMMARY:A"), "Cal", w).events[0]
    other = dataclasses.replace(first, title="B", start=first.start + dt.timedelta(hours=1))
    found: dict[str, tuple[Event, int]] = {}
    for event in (first, first, other, other):
        ics._keep_unique(found, event, 0)
    assert [event.title for event, _index in found.values()] == ["A", "B"]
    assert len(found) == 2


def test_the_deadline_also_stops_a_rule_mid_iteration() -> None:
    """The clock is read inside dateutil's iterator too, so one costly rule cannot outlast
    the budget."""
    readings = iter([0.0, 0.0])  # the start, the check before the series; then late
    impossible = "RRULE:FREQ=DAILY;BYMONTH=2;BYMONTHDAY=30"
    data = calendar(f"UID:i\nDTSTART:19710101T090000Z\n{impossible}\nSUMMARY:Never")
    result = expand(
        data,
        "Cal",
        window("2026-10-23", 1, "UTC"),
        budget_s=1.0,
        clock=lambda: next(readings, 99.0),
    )
    assert result.truncated == 1
    assert result.skipped == 0


def test_too_many_components(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ics, "MAX_COMPONENTS", 2)
    data = calendar(*(f"UID:e{n}\nDTSTART:20261023T090000Z" for n in range(3)))
    with pytest.raises(IcsError, match="too_many_events"):
        expand(data, "Cal", window("2026-10-23", 1, "UTC"))
