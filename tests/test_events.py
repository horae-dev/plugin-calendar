# SPDX-License-Identifier: Apache-2.0
"""The event model: windows, text cleaning, ids; Hypothesis properties of the expansion."""

import datetime as dt
import re
import unicodedata

import icalendar
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from conftest import NOW, feed, window
from horae_calendar.events import (
    MAX_DESCRIPTION,
    MAX_DESCRIPTION_LINES,
    MAX_TITLE,
    Window,
    WindowError,
    clean_line,
    clean_text,
    event_id,
    instance_key,
)
from horae_calendar.ics import expand

FINGERPRINT = re.compile(r"[A-Za-z0-9._:-]{1,128}")


def test_window_whole_days_in_the_time_zone() -> None:
    w = window("2026-10-24", 2)
    assert w.to_json() == {
        "start": "2026-10-24T00:00:00+02:00",
        "end": "2026-10-26T00:00:00+01:00",
        "timezone": "Europe/Paris",
        "days": 2,
    }


def test_window_defaults_to_today_in_the_time_zone() -> None:
    late = dt.datetime(2026, 10, 20, 23, 30, tzinfo=dt.UTC)
    assert Window.build(None, 1, "Europe/Paris", late).first_day == dt.date(2026, 10, 21)
    assert Window.build(None, 1, "UTC", late).first_day == dt.date(2026, 10, 20)


def test_window_midnight_in_a_gap_is_normalised() -> None:
    """America/Santiago skips 00:00 on 6 Sep 2026 (the clocks go to 01:00)."""
    w = window("2026-09-06", 1, "America/Santiago")
    assert w.to_json()["start"] == "2026-09-06T01:00:00-03:00"


@pytest.mark.parametrize(
    ("start", "days", "tz", "message"),
    [
        ("2026-10-24", 0, "UTC", "days"),
        ("2026-10-24", 63, "UTC", "days"),
        ("2026-10-24", True, "UTC", "days"),
        ("2026-10-24", 1, "Mars/Olympus", "timezone"),
        ("2026-10-24", 1, "../../etc/passwd", "timezone"),
        ("2026-02-30", 1, "UTC", "real date"),
        ("24/10/2026", 1, "UTC", "YYYY-MM-DD"),
        ("0001-01-01", 1, "UTC", "out of range"),
    ],
)
def test_window_refuses_bad_parameters(start: str, days: int, tz: str, message: str) -> None:
    with pytest.raises(WindowError, match=message):
        Window.build(start, days, tz, NOW)


def test_instance_keys() -> None:
    assert instance_key(dt.date(2026, 10, 26)) == "20261026"
    paris = window("2026-10-26", 1).tz
    assert instance_key(dt.datetime(2026, 10, 26, 9, 30, tzinfo=paris)) == "20261026T083000Z"
    assert instance_key(dt.datetime(2026, 10, 26, 9, 30)) == "20261026T093000"


def test_event_ids_use_the_sdk_fingerprint() -> None:
    assert event_id("abc", None) == "calendar:abc"
    assert event_id("abc", "20261026") == "calendar:abc:20261026"
    assert event_id("a@b", "20261026") != event_id("a-b", "20261026")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Team standup", "Team standup"),
        ("  two\r\nlines\u2028here\tand  there ", "two lines here and there"),
        ("bidi\u202eand\u200bzero width\x07", "bidiandzero width"),
        ("[[link]] #tag 📅 2020-01-01", "[[link]] #tag 📅 2020-01-01"),
        ("Pay &#x1F4C5; 2020-01-01", "Pay &#x1F4C5; 2020-01-01"),
        (
            "**bold** _it_ `code` <b>x</b> \\[x\\] &amp;",
            "**bold** _it_ `code` <b>x</b> \\[x\\] &amp;",
        ),
        ("👩\u200d💻 dev, \u0915\u094d\u200c\u0937", "👩\u200d💻 dev, \u0915\u094d\u200c\u0937"),
        ("", None),
        ("\u200b\n\t", None),
        ("\u200d", None),
        (None, None),
        (42, None),
    ],
)
def test_clean_line(raw: object, expected: str | None) -> None:
    assert clean_line(raw, 100) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("one\r\ntwo\rthree\u2028four", "one\ntwo\nthree\nfour"),
        ("\n\n  a  \t b \n\n\n\nc\n\n", "a b\n\nc"),
        ("x\x1b[31mred\u202e\x00", "x[31mred"),
        ("- [ ] task 📅 2020-01-01\n# heading", "- [ ] task 📅 2020-01-01\n# heading"),
        ("\n\u200b\n", None),
    ],
)
def test_clean_text_keeps_line_breaks(raw: str, expected: str | None) -> None:
    assert clean_text(raw, 100) == expected


def test_clean_text_bounds_the_lines() -> None:
    text = clean_text("\n".join(f"line {n}" for n in range(200)), 10_000)
    assert text is not None
    assert text.split("\n") == [f"line {n}" for n in range(MAX_DESCRIPTION_LINES)]


def test_the_caps_count_the_raw_characters() -> None:
    """Nothing is escaped first, so `&` and `[` count one character each."""
    assert clean_line("&[" * 400, MAX_TITLE) == ("&[" * 400)[:MAX_TITLE]
    assert clean_text("&" * 1500, MAX_DESCRIPTION) == "&" * MAX_DESCRIPTION
    assert clean_line("x" * 5000, MAX_TITLE) == "x" * MAX_TITLE


ALLOWED_FORMAT = {"\u200c", "\u200d"}


def assert_safe(text: str, max_length: int, *, line_breaks: bool) -> None:
    assert len(text) <= max_length
    assert text == text.strip()
    assert "\r" not in text
    if not line_breaks:
        assert "\n" not in text
        assert "  " not in text
    for char in text:
        category = unicodedata.category(char)
        if char == "\n":
            continue
        assert category not in {"Cc", "Cs", "Zl", "Zp"}, repr(char)
        assert category != "Cf" or char in ALLOWED_FORMAT, repr(char)


@given(st.text(max_size=2000))
def test_clean_line_is_always_safe(raw: str) -> None:
    text = clean_line(raw, 200)
    if text is not None:
        assert_safe(text, 200, line_breaks=False)


@given(st.text(max_size=2000))
def test_clean_text_is_always_safe(raw: str) -> None:
    text = clean_text(raw, 500)
    if text is not None:
        assert_safe(text, 500, line_breaks=True)
        assert text.count("\n") < MAX_DESCRIPTION_LINES
        assert "\n\n\n" not in text


def ics_with_summary(summary: str) -> bytes:
    calendar = icalendar.Calendar()
    calendar.add("prodid", "-//t//EN")
    calendar.add("version", "2.0")
    event = icalendar.Event()
    event.add("uid", "prop@example.org")
    event.add("dtstamp", dt.datetime(2026, 1, 1, tzinfo=dt.UTC))
    event.add("dtstart", dt.datetime(2026, 10, 23, 9, tzinfo=dt.UTC))
    event.add("summary", summary)
    calendar.add_component(event)
    data: bytes = calendar.to_ical()
    return data


@settings(max_examples=60, deadline=None)
@given(st.text(max_size=300))
def test_any_summary_becomes_a_safe_title(summary: str) -> None:
    (event,) = expand(ics_with_summary(summary), "Cal", window("2026-10-23", 1, "UTC")).events
    assert event.title == (clean_line(summary, MAX_TITLE) or "(untitled)")
    assert FINGERPRINT.fullmatch(event.id)


@settings(max_examples=40, deadline=None)
@given(
    offset=st.integers(min_value=0, max_value=40),
    first=st.integers(min_value=1, max_value=20),
    second=st.integers(min_value=1, max_value=20),
)
def test_windows_add_up(offset: int, first: int, second: int) -> None:
    """The ids of a window are those of its two halves (no instance lost or doubled at a
    boundary, the DST switch of 25 Oct included)."""
    start = dt.date(2026, 10, 1) + dt.timedelta(days=offset)
    middle = start + dt.timedelta(days=first)

    def ids(day: dt.date, days: int) -> set[str]:
        events = expand(feed("overrides"), "C", window(day.isoformat(), days)).events
        events += expand(feed("weekly-exdate"), "C", window(day.isoformat(), days)).events
        return {e.id for e in events}

    assert ids(start, first + second) == ids(start, first) | ids(middle, second)


@settings(max_examples=60, deadline=None)
@given(
    interval=st.integers(min_value=1, max_value=5),
    count=st.integers(min_value=1, max_value=40),
    offset=st.integers(min_value=0, max_value=40),
    days=st.integers(min_value=1, max_value=15),
)
def test_daily_rule_matches_arithmetic(interval: int, count: int, offset: int, days: int) -> None:
    """FREQ=DAILY;INTERVAL;COUNT at 09:00 Paris from 1 Oct: the instances in a window are
    exactly the arithmetic ones, each at 09:00 local whatever the offset."""
    data = (
        "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//t//EN\nBEGIN:VEVENT\nUID:d\n"
        "DTSTAMP:20260101T000000Z\nDTSTART;TZID=Europe/Paris:20261001T090000\n"
        f"RRULE:FREQ=DAILY;INTERVAL={interval};COUNT={count}\nSUMMARY:D\nEND:VEVENT\n"
        "END:VCALENDAR\n"
    ).encode()
    first = dt.date(2026, 10, 1) + dt.timedelta(days=offset)
    w = window(first.isoformat(), days)
    found = sorted(expand(data, "C", w).events, key=lambda e: e.sort_key(w))
    expected = [
        dt.date(2026, 10, 1) + dt.timedelta(days=k * interval)
        for k in range(count)
        if first <= dt.date(2026, 10, 1) + dt.timedelta(days=k * interval) < w.end_day
    ]
    assert [e.start.date() for e in found] == expected  # type: ignore[union-attr]
    assert all(e.start.hour == 9 for e in found)  # type: ignore[union-attr]
