# SPDX-License-Identifier: Apache-2.0
"""`calendar.prep_tasks`: rules match or not, one task per occurrence, lead time across DST
and month ends, hostile titles escaped, deterministic output and stable fingerprints."""

import json
from typing import Any

import anyio
import pytest
from horae_sdk import Plugin
from horae_sdk.testing import call

from conftest import feed, window
from horae_calendar.ics import expand
from horae_calendar.prep import PrepError, prep_tasks, task_key

TODAY = "2026-10-20"
RULES: list[dict[str, Any]] = [{"keywords": ["review", "trip"]}]


def ev(
    title: str = "Quarterly review",
    start: str = "2026-10-22T09:30:00+02:00",
    end: str | None = None,
    *,
    id: str = "calendar:e1",
    calendar: str = "Work",
    all_day: bool = False,
) -> dict[str, Any]:
    return {
        "id": id,
        "calendar": calendar,
        "title": title,
        "start": start,
        "end": end or start,
        "all_day": all_day,
        "status": "confirmed",
    }


def src(*events: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"source": "calendar.events", "data": {"events": list(events), "complete": True}}]


def run(
    events: list[dict[str, Any]],
    rules: list[dict[str, Any]] | None = None,
    today: str = TODAY,
    **extra: Any,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    return prep_tasks({"today": today, "rules": rules or RULES, **extra}, src(*events))


def test_matching_event_gets_a_task_due_the_day_before() -> None:
    data, intents = run([ev()])
    assert intents == [
        {
            "type": "task.create",
            "title": "Prepare: Quarterly review",
            "fingerprint": "calendar:prep:calendar:e1",
            "due": "2026-10-21",
        }
    ]
    assert data["tasks"] == 1


def test_no_match_no_task() -> None:
    data, intents = run([ev("Lunch")])
    assert intents == []
    assert (data["events"], data["matched"], data["tasks"]) == (1, 0, 0)


def test_keywords_ignore_case_and_regex_matches() -> None:
    assert len(run([ev("BIG TRIP")])[1]) == 1
    rules = [{"regex": r"^q\d review$"}]
    assert len(run([ev("Q3 review")], rules)[1]) == 1
    assert run([ev("Q3 review now")], rules)[1] == []


def test_calendars_filter_and_first_rule_wins() -> None:
    rules: list[dict[str, Any]] = [
        {"keywords": ["review"], "calendars": ["Family"], "title": "Family: ${title}"},
        {"keywords": ["review"], "lead_days": 2, "title": "Work: ${title}"},
    ]
    intents = run([ev(), ev(id="calendar:e2", calendar="Family")], rules)[1]
    assert [(i["title"], i["due"]) for i in intents] == [
        ("Work: Quarterly review", "2026-10-20"),
        ("Family: Quarterly review", "2026-10-21"),
    ]


def test_recurring_occurrences_each_get_their_own_task() -> None:
    result = expand(feed("weekly-exdate"), "Work", window("2026-10-15", 14))
    events = [e.to_json() for e in result.events]
    assert len(events) >= 3
    intents = run(events, [{"keywords": ["standup"]}], today="2026-10-14")[1]
    assert len(intents) == len(events)
    assert len({i["fingerprint"] for i in intents}) == len(events)
    assert len({i["due"] for i in intents}) == len(events)


@pytest.mark.parametrize(
    ("start", "lead", "due"),
    [
        ("2026-03-29T00:30:00+01:00", 1, "2026-03-28"),  # the spring-forward day
        ("2026-03-29T09:00:00+02:00", 1, "2026-03-28"),
        ("2026-10-25T00:30:00+02:00", 1, "2026-10-24"),  # the fall-back day
        ("2026-10-26T09:00:00+01:00", 2, "2026-10-24"),  # across the change
        ("2026-03-01T09:00:00+01:00", 1, "2026-02-28"),  # a month end
        ("2028-03-01T09:00:00+01:00", 1, "2028-02-29"),  # a leap year
        ("2027-01-02T09:00:00+01:00", 3, "2026-12-30"),  # a year end
        ("2026-11-30T23:30:00-08:00", 0, "2026-11-30"),  # another zone: its own date
    ],
)
def test_lead_time_is_calendar_days_in_the_event_zone(start: str, lead: int, due: str) -> None:
    intents = run([ev(start=start)], [{"keywords": ["review"], "lead_days": lead}], "2026-01-01")[1]
    assert intents[0]["due"] == due


def test_all_day_events_use_their_dates() -> None:
    trip = ev("Trip", "2026-11-01", "2026-11-04", all_day=True)
    assert run([trip], today="2026-10-01")[1][0]["due"] == "2026-10-31"
    assert run([trip], today="2026-11-03")[1][0]["due"] == "2026-11-03"  # still going
    data, intents = run([trip], today="2026-11-04")  # ended
    assert intents == []
    assert data["past"] == 1


def test_due_in_the_past_becomes_today() -> None:
    assert run([ev(start="2026-10-20T08:00:00+02:00")])[1][0]["due"] == TODAY


def test_hostile_titles_are_escaped_and_cannot_break_out() -> None:
    title = "review 📅 2020-01-01 [x](http://e.test) #tag\n- [ ] injected ‮\x00"
    template = "Prep ${title} ${title}"
    intents = run([ev(title)], [{"keywords": ["review"], "title": template}])[1]
    out = intents[0]["title"]
    assert "\n" not in out
    assert "‮" not in out
    assert "📅 2020-01-01" not in out
    assert out.count("injected") == 1  # the placeholder is replaced once
    assert "&#x1F4C5;" in out  # the emoji is neutralised, not left to start a date


def test_deterministic_and_stable_fingerprints_across_runs() -> None:
    events = [ev(id="calendar:b", start="2026-10-25T10:00:00+01:00"), ev(), ev("Lunch", id="z")]
    first = run(events)
    assert json.dumps(first) == json.dumps(run(list(reversed(events))))
    assert [i["fingerprint"] for i in first[1]] == [
        "calendar:prep:calendar:e1",
        "calendar:prep:calendar:b",
    ]
    assert task_key("calendar:e1") == "calendar:prep:calendar:e1"


def test_long_or_odd_event_ids_still_give_valid_fingerprints() -> None:
    odd = "calendar:" + "é" * 150
    key = task_key(odd)
    assert len(key) <= 128
    assert key == task_key(odd)
    assert key != task_key(odd + "x")
    assert run([ev(id=odd)])[1][0]["fingerprint"] == key


def test_bounds_and_duplicates_and_bad_entries() -> None:
    events = [ev(id=f"e{n}", start=f"2026-11-{n + 1:02d}T10:00:00+01:00") for n in range(5)]
    data, intents = run(events, max_tasks=2)
    assert [i["fingerprint"] for i in intents] == ["calendar:prep:e0", "calendar:prep:e1"]
    assert (data["matched"], data["tasks"], data["left_out"]) == (5, 2, 3)
    data, intents = prep_tasks(
        {"today": TODAY, "rules": RULES},
        src(ev(), ev(), {"id": 3}, ev(start="not a date", id="bad")),
    )
    assert (len(intents), data["skipped"]) == (1, 2)


@pytest.mark.parametrize(
    "params",
    [
        {"rules": RULES},
        {"today": "2026-13-01", "rules": RULES},
        {"today": "20261020", "rules": RULES},
        {"today": TODAY},
        {"today": TODAY, "rules": []},
        {"today": TODAY, "rules": RULES, "extra": 1},
        {"today": TODAY, "rules": RULES, "max_tasks": 0},
        {"today": TODAY, "rules": RULES, "max_tasks": True},
        {"today": TODAY, "rules": [{}]},
        {"today": TODAY, "rules": ["x"]},
        {"today": TODAY, "rules": [{"keywords": [""]}]},
        {"today": TODAY, "rules": [{"keywords": "a"}]},
        {"today": TODAY, "rules": [{"regex": "("}]},
        {"today": TODAY, "rules": [{"regex": "(a+)+$"}]},
        {"today": TODAY, "rules": [{"regex": 3}]},
        {"today": TODAY, "rules": [{"keywords": ["a"], "lead_days": 61}]},
        {"today": TODAY, "rules": [{"keywords": ["a"], "lead_days": 1.5}]},
        {"today": TODAY, "rules": [{"keywords": ["a"], "calendars": []}]},
        {"today": TODAY, "rules": [{"keywords": ["a"], "title": "a\nb"}]},
        {"today": TODAY, "rules": [{"keywords": ["a"], "colour": "red"}]},
    ],
)
def test_bad_parameters_fail(params: dict[str, Any]) -> None:
    with pytest.raises(PrepError):
        prep_tasks(params, src())


@pytest.mark.parametrize("bad", [None, {}, [], [{"source": "obsidian.open_tasks"}]])
def test_bad_input_fails(bad: Any) -> None:
    with pytest.raises(PrepError):
        prep_tasks({"today": TODAY, "rules": RULES}, bad)
    with pytest.raises(PrepError):
        prep_tasks({"today": TODAY, "rules": RULES}, [{"source": "calendar.events", "data": {}}])


def test_through_mcp(plugin: Plugin) -> None:
    async def go() -> Any:
        return await call(
            plugin, "calendar.prep_tasks", {"today": TODAY, "rules": RULES}, input=src(ev())
        )

    replayed = anyio.run(go)
    assert replayed.error is None, replayed.error
    assert [i["type"] for i in replayed.intents] == ["task.create"]


def test_dates_at_the_ends_of_the_calendar_never_crash_the_transform() -> None:
    """A feed can hold any year: an all-day event ending on 0001-01-01 (its last day would be
    before date.min) is skipped, and one on 9999-12-31 gets its task, without OverflowError."""
    events = [
        ev(id="calendar:min", start="0001-01-01", end="0001-01-01", all_day=True),
        ev(id="calendar:max", start="9999-12-31", end="9999-12-31", all_day=True),
        ev(id="calendar:min-timed", start="0001-01-01T00:30:00+02:00"),
    ]
    data, intents = run(events, rules=[{"keywords": ["review"], "lead_days": 30}])
    assert [i["fingerprint"] for i in intents] == ["calendar:prep:calendar:max"]
    assert intents[0]["due"] == "9999-12-01"
    assert data["tasks"] == 1
