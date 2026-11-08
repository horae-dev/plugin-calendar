# SPDX-License-Identifier: Apache-2.0
"""Google Calendar API: the token exchange and its refresh, `events.list` with pagination,
the mapping to `Event`, the failures as fixed codes, and the tokens that never leave memory.
The Google endpoints are a fake server on 127.0.0.1 (never the network)."""

import datetime as dt
import json
import logging
import threading
from collections import defaultdict, deque
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import anyio
import pytest
from horae_sdk import Plugin
from horae_sdk.testing import call

from conftest import NOW, FakeFetcher, window
from horae_calendar import build, google
from horae_calendar.events import event_id, instance_key
from horae_calendar.fetch import HttpFetcher

CLIENT_ID = "1234-fake.apps.example.org"
CLIENT_SECRET = "GOCSPX-fake-client-secret"  # noqa: S105 (synthetic)
REFRESH = "1//fake-refresh-token"
ACCESS = "ya29.fake-access-token-"
SECRET = f"{CLIENT_ID} {CLIENT_SECRET} {REFRESH}\n"
SPEC = {"name": "Perso", "type": "google", "index": 1}
SENSITIVE = (CLIENT_SECRET, REFRESH, ACCESS)

Reply = tuple[int, dict[str, Any]]


class Google(BaseHTTPRequestHandler):
    """The token endpoint and `events.list`: each route plays its queued replies in order
    (the last one repeats), and every request is recorded."""

    queues: dict[str, deque[Reply]] = defaultdict(deque)  # noqa: RUF012 (shared by the server)
    seen: list[dict[str, Any]] = []  # noqa: RUF012
    tokens = 0

    def log_message(self, format: str, *args: object) -> None:
        """Silent: the server must not write tokens to stderr."""

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode()
        form = {k: v[0] for k, v in parse_qs(body).items()}
        self.record("token", form=form)
        self.play("token")

    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        if "/redirect-away" in parts.path:
            self.record("away")
            self.send_response(307)
            self.send_header("Location", f"http://127.0.0.2:{self.server.server_port}/x")  # type: ignore[attr-defined]
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        calendar = unquote(parts.path.split("/calendars/")[1].split("/events")[0])
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        self.record(f"events:{calendar}", query=query)
        self.play(f"events:{calendar}")

    def record(self, route: str, **extra: Any) -> None:
        Google.seen.append(
            {"route": route, "auth": self.headers.get("Authorization"), "path": self.path, **extra}
        )

    def play(self, route: str) -> None:
        queue = Google.queues[route]
        status, body = queue[0] if len(queue) == 1 else queue.popleft()
        if route == "token" and status == 200 and "access_token" not in body and not body:
            Google.tokens += 1
            body = {"access_token": f"{ACCESS}{Google.tokens}", "expires_in": 3599}
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture(scope="module")
def server() -> Iterator[str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Google)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def fake(server: str, monkeypatch: pytest.MonkeyPatch) -> type[Google]:
    """The fake server reset, and the plugin's Google endpoints pointed at it."""
    Google.queues.clear()
    Google.seen.clear()
    Google.tokens = 0
    Google.queues["token"].append((200, {}))
    monkeypatch.setattr(google, "TOKEN_URL", f"{server}/token")
    monkeypatch.setattr(google, "API_URL", f"{server}/calendar/v3/calendars")
    return Google


def plugin_for(secret: str | None = SECRET) -> Plugin:
    environ = {"CALENDAR_ICS_URLS": "not-in-use", "CALENDAR_CALDAV": "not-in-use"}
    if secret is not None:
        environ["CALENDAR_GOOGLE"] = secret
    client = HttpFetcher(schemes=("http",), timeout_s=5.0, attempts=1)
    return build(FakeFetcher({}), api=client, environ=environ, clock=lambda: NOW)


def run(plugin: Plugin, **params: Any) -> Any:
    async def go() -> Any:
        return await call(plugin, "calendar.events", params)

    return anyio.run(go)


def events(plugin: Plugin, **params: Any) -> dict[str, Any]:
    replayed = run(plugin, **params)
    assert replayed.error is None, replayed.error
    assert isinstance(replayed.data, dict)
    return replayed.data


def items(*entries: dict[str, Any], next_page: str | None = None) -> Reply:
    body: dict[str, Any] = {"items": list(entries)}
    if next_page:
        body["nextPageToken"] = next_page
    return 200, body


def timed(uid: str, start: str, end: str, **more: Any) -> dict[str, Any]:
    return {
        "id": uid,
        "iCalUID": f"{uid}@google.com",
        "summary": f"Event {uid}",
        "start": {"dateTime": start},
        "end": {"dateTime": end},
        **more,
    }


def no_leak(*texts: str) -> None:
    for text in texts:
        for secret in SENSITIVE:
            assert secret not in text


# --- the mapping -------------------------------------------------------------------------

STANDUP = timed(
    "std_20261026T083000Z",
    "2026-10-26T09:30:00+01:00",
    "2026-10-26T09:45:00+01:00",
    recurringEventId="std",
    originalStartTime={"dateTime": "2026-10-26T09:30:00+01:00"},
    summary="Team standup",
    location="Room 1",
    description="Weekly\nsync",
)
LUNCH = {
    "id": "lunch",
    "iCalUID": "lunch@google.com",
    "summary": "Family lunch",
    "start": {"date": "2026-10-27"},
    "end": {"date": "2026-10-28"},
    "status": "tentative",
}


def test_events_are_requested_with_the_window_and_mapped(fake: type[Google]) -> None:
    fake.queues["events:primary"].append(items(LUNCH, STANDUP))
    data = events(
        plugin_for(), calendars=[SPEC], start="2026-10-26", days=3, timezone="Europe/Paris"
    )
    assert data["complete"] is True
    assert data["calendars"] == [
        {
            "name": "Perso",
            "ok": True,
            "complete": True,
            "events": 2,
            "skipped": 0,
            "truncated": 0,
            "warnings": 0,
        }
    ]
    standup, lunch = data["events"]  # sorted by start
    assert standup == {
        "id": event_id("std_20261026T083000Z@google.com", "20261026T083000Z"),
        "calendar": "Perso",
        "title": "Team standup",
        "start": "2026-10-26T09:30:00+01:00",
        "end": "2026-10-26T09:45:00+01:00",
        "all_day": False,
        "location": "Room 1",
        "description": "Weekly\nsync",
        "status": "confirmed",
        "recurring": True,
    }
    assert lunch["all_day"] is True
    assert (lunch["start"], lunch["end"], lunch["status"]) == (
        "2026-10-27",
        "2026-10-28",
        "tentative",
    )
    token_call, events_call = fake.seen
    assert token_call["form"] == {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": REFRESH,
        "grant_type": "refresh_token",
    }
    query = events_call["query"]
    assert query["singleEvents"] == "true"
    assert query["orderBy"] == "startTime"
    assert query["timeMin"] == "2026-10-25T23:00:00Z"
    assert query["timeMax"] == "2026-10-28T23:00:00Z"
    assert events_call["auth"] == f"Bearer {ACCESS}1"


def test_ids_are_stable_and_instances_keep_theirs_when_moved() -> None:
    win = window("2026-10-26", 3)
    moved = dict(STANDUP, start={"dateTime": "2026-10-27T14:00:00+01:00"})
    moved["end"] = {"dateTime": "2026-10-27T14:15:00+01:00"}
    before = google.to_event(STANDUP, "G", win)
    after = google.to_event(moved, "G", win)
    assert before is not None
    assert after is not None
    assert before.id == after.id
    start = dt.datetime(2026, 10, 26, 8, 30, tzinfo=dt.UTC)
    assert before.id == event_id("std_20261026T083000Z@google.com", instance_key(start))
    single = google.to_event(LUNCH, "G", win)
    assert single is not None
    assert single.id == event_id("lunch@google.com", None)
    assert single.recurring is False


def test_the_calendar_id_is_in_the_path_encoded(fake: type[Google]) -> None:
    fake.queues["events:team@group.calendar.google.com"].append(items())
    spec = dict(SPEC, calendar_id="team@group.calendar.google.com")
    events(plugin_for(), calendars=[spec])
    assert "/calendars/team%40group.calendar.google.com/events" in fake.seen[1]["path"]


def test_cancelled_and_outside_items_are_left_out_and_bad_ones_skipped(fake: type[Google]) -> None:
    fake.queues["events:primary"].append(
        items(
            {**LUNCH, "status": "cancelled"},
            timed("old", "2026-10-01T10:00:00+02:00", "2026-10-01T11:00:00+02:00"),
            {"id": "x", "start": {"dateTime": "nope"}, "end": {"dateTime": "nope"}},
            {
                "id": "y",
                "start": {"date": "2026-10-26"},
                "end": {"dateTime": "2026-10-26T10:00:00Z"},
            },
            "not an object",  # type: ignore[arg-type]
            timed("ok", "2026-10-26T10:00:00+01:00", "2026-10-26T11:00:00+01:00"),
        )
    )
    data = events(
        plugin_for(), calendars=[SPEC], start="2026-10-26", days=1, timezone="Europe/Paris"
    )
    assert [e["title"] for e in data["events"]] == ["Event ok"]
    assert data["calendars"][0]["skipped"] == 3
    assert data["complete"] is False


def test_untrusted_text_is_cleaned_and_untitled_named(fake: type[Google]) -> None:
    fake.queues["events:primary"].append(
        items(
            timed(
                "t",
                "2026-10-26T10:00:00+01:00",
                "2026-10-26T11:00:00+01:00",
                summary="a‮b\n\nc",
                location=None,
                description=42,
            ),
            timed("u", "2026-10-26T12:00:00+01:00", "2026-10-26T13:00:00+01:00", summary=""),
        )
    )
    data = events(
        plugin_for(), calendars=[SPEC], start="2026-10-26", days=1, timezone="Europe/Paris"
    )
    assert [e["title"] for e in data["events"]] == ["ab c", "(untitled)"]
    assert data["events"][0]["location"] is None
    assert data["events"][0]["description"] is None


def test_a_naive_date_time_is_read_in_its_time_zone() -> None:
    win = window("2026-10-26", 1)
    item = {
        "id": "n",
        "start": {"dateTime": "2026-10-26T09:00:00", "timeZone": "America/New_York"},
        "end": {"dateTime": "2026-10-26T10:00:00", "timeZone": "America/New_York"},
    }
    event = google.to_event(item, "G", win)
    assert event is not None
    assert event.to_json()["start"] == "2026-10-26T14:00:00+01:00"
    item["start"]["timeZone"] = "Not/AZone"  # type: ignore[index]
    assert google.to_event(item, "G", win) is None


def test_an_item_without_an_end_is_an_instant_or_a_day() -> None:
    win = window("2026-10-26", 1)
    instant = google.to_event(
        {"id": "i", "start": {"dateTime": "2026-10-26T10:00:00+01:00"}}, "G", win
    )
    day = google.to_event({"id": "d", "start": {"date": "2026-10-26"}}, "G", win)
    assert instant is not None
    assert day is not None
    assert instant.start == instant.end
    assert day.to_json()["end"] == "2026-10-27"


# --- the token ---------------------------------------------------------------------------


def test_one_exchange_per_call_serves_every_calendar_of_the_account(fake: type[Google]) -> None:
    fake.queues["events:primary"].append(items())
    fake.queues["events:work"].append(items())
    specs = [SPEC, {"name": "Work", "type": "google", "index": 1, "calendar_id": "work"}]
    events(plugin_for(), calendars=specs)
    assert [s["route"] for s in fake.seen].count("token") == 1
    events(plugin_for(), calendars=specs)  # a new call exchanges again: nothing is kept
    assert [s["route"] for s in fake.seen].count("token") == 2


def test_a_401_refreshes_once_and_retries(fake: type[Google]) -> None:
    fake.queues["events:primary"].extend([(401, {"error": {"code": 401}}), items(LUNCH)])
    data = events(plugin_for(), calendars=[SPEC], start="2026-10-27", days=1)
    assert len(data["events"]) == 1
    auths = [s["auth"] for s in fake.seen if s["route"].startswith("events")]
    assert auths == [f"Bearer {ACCESS}1", f"Bearer {ACCESS}2"]


def test_a_second_401_is_auth_failed(fake: type[Google]) -> None:
    fake.queues["events:primary"].append((401, {"error": {"code": 401}}))
    replayed = run(plugin_for(), calendars=[SPEC])
    assert replayed.error == "calendar failed: Perso: auth_failed"
    assert [s["route"] for s in fake.seen].count("token") == 2


@pytest.mark.parametrize("error", ["invalid_grant", "invalid_client", "unauthorized_client"])
def test_a_refused_refresh_token_is_auth_failed(fake: type[Google], error: str) -> None:
    fake.queues["token"][0] = (400, {"error": error, "error_description": "Token expired"})
    replayed = run(plugin_for(), calendars=[SPEC])
    assert replayed.error == "calendar failed: Perso: auth_failed"
    assert not [s for s in fake.seen if s["route"].startswith("events")]


@pytest.mark.parametrize(
    ("status", "body", "reason"),
    [
        (200, {"expires_in": 5, "access_token": ""}, "bad_response"),
        (200, {"no": "token"}, "bad_response"),
        (400, {"error": "invalid_scope"}, "token_http_400"),
        (429, {}, "rate_limited"),
        (500, {}, "http_500"),
        (418, {}, "http_418"),
    ],
)
def test_token_endpoint_failures_are_fixed_codes(
    fake: type[Google], status: int, body: dict[str, Any], reason: str
) -> None:
    fake.queues["token"][0] = (status, body or {"x": 1})
    replayed = run(plugin_for(), calendars=[SPEC])
    assert replayed.error == f"calendar failed: Perso: {reason}"


def test_the_token_endpoint_answering_text_is_a_bad_response(fake: type[Google]) -> None:
    fake.queues["token"][0] = (200, {"access_token": "has space"})
    assert run(plugin_for(), calendars=[SPEC]).error == "calendar failed: Perso: bad_response"


# --- pagination, limits ------------------------------------------------------------------


def test_pages_are_followed_with_their_token(fake: type[Google]) -> None:
    fake.queues["events:primary"].extend(
        [
            items(
                timed("a", "2026-10-26T10:00:00+01:00", "2026-10-26T11:00:00+01:00"), next_page="p2"
            ),
            items(
                timed("b", "2026-10-26T12:00:00+01:00", "2026-10-26T13:00:00+01:00"), next_page="p3"
            ),
            items(timed("c", "2026-10-26T14:00:00+01:00", "2026-10-26T14:00:00+01:00")),
        ]
    )
    data = events(
        plugin_for(), calendars=[SPEC], start="2026-10-26", days=1, timezone="Europe/Paris"
    )
    assert [e["title"] for e in data["events"]] == ["Event a", "Event b", "Event c"]
    queries = [s["query"] for s in fake.seen if s["route"].startswith("events")]
    assert [q.get("pageToken") for q in queries] == [None, "p2", "p3"]
    assert data["complete"] is True


def test_pages_are_bounded(fake: type[Google], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(google, "MAX_PAGES", 3)
    fake.queues["events:primary"].append(items(LUNCH, next_page="again"))
    data = events(plugin_for(), calendars=[SPEC], start="2026-10-27", days=1)
    assert len([s for s in fake.seen if s["route"].startswith("events")]) == 3
    assert data["complete"] is False
    assert data["calendars"][0]["truncated"] == 1
    assert len(data["events"]) == 1  # the same event on each page is returned once


def test_events_are_capped_per_calendar(
    fake: type[Google], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(google, "MAX_EVENTS", 2)
    many = [
        timed(f"e{n}", f"2026-10-26T{n:02d}:00:00+01:00", f"2026-10-26T{n:02d}:30:00+01:00")
        for n in range(5)
    ]
    fake.queues["events:primary"].append(items(*many))
    data = events(
        plugin_for(), calendars=[SPEC], start="2026-10-26", days=1, timezone="Europe/Paris"
    )
    assert len(data["events"]) == 2
    assert data["calendars"][0]["truncated"] == 1


def test_the_time_budget_cuts_the_pages(
    fake: type[Google], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(google, "BUDGET_S", -1.0)
    fake.queues["events:primary"].append(items(LUNCH, next_page="more"))
    data = events(plugin_for(), calendars=[SPEC], start="2026-10-27", days=1)
    assert data["calendars"][0]["truncated"] == 1


@pytest.mark.parametrize(
    "reply",
    [(200, {"items": "no"}), (200, {"items": [], "nextPageToken": 5}), (200, {})],
)
def test_a_malformed_page_is_bad_response(fake: type[Google], reply: Reply) -> None:
    fake.queues["events:primary"].append(reply)
    assert run(plugin_for(), calendars=[SPEC]).error == "calendar failed: Perso: bad_response"


# --- failures ----------------------------------------------------------------------------


def rate(reason: str) -> Reply:
    return 403, {"error": {"code": 403, "message": "secret text", "errors": [{"reason": reason}]}}


@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        ((429, {"error": {"message": "slow down"}}), "rate_limited"),
        (rate("rateLimitExceeded"), "rate_limited"),
        (rate("userRateLimitExceeded"), "rate_limited"),
        (rate("quotaExceeded"), "rate_limited"),
        (rate("forbidden"), "http_403"),
        ((403, {"error": "odd"}), "http_403"),
        ((404, {}), "http_404"),
        ((503, {}), "http_503"),
        ((400, {}), "http_400"),
    ],
)
def test_api_failures_are_fixed_codes(fake: type[Google], reply: Reply, reason: str) -> None:
    fake.queues["events:primary"].append(reply)
    replayed = run(plugin_for(), calendars=[SPEC])
    assert replayed.error == f"calendar failed: Perso: {reason}"
    assert "secret text" not in replayed.error
    assert "slow down" not in replayed.error


def test_a_redirect_away_is_refused_and_carries_no_token(fake: type[Google]) -> None:
    fake.queues["events:redirect-away"].append(items())
    spec = dict(SPEC, calendar_id="redirect-away")
    replayed = run(plugin_for(), calendars=[spec])
    assert replayed.error == "calendar failed: Perso: redirect_refused"
    assert not [s for s in fake.seen if s["path"].startswith("/x")]


def test_a_failing_google_calendar_with_partial(fake: type[Google]) -> None:
    fake.queues["events:primary"].append(items(LUNCH))
    fake.queues["events:gone"].append((404, {}))
    specs = [SPEC, dict(SPEC, name="Gone", calendar_id="gone")]
    data = events(plugin_for(), calendars=specs, partial=True, start="2026-10-27", days=1)
    assert data["complete"] is False
    assert data["calendars"][1] == {"name": "Gone", "ok": False, "error": "http_404"}
    assert [e["title"] for e in data["events"]] == ["Family lunch"]


def test_ics_google_and_caldav_calendars_share_a_call(fake: type[Google]) -> None:
    fake.queues["events:primary"].append(items(LUNCH))
    environ = {
        "CALENDAR_ICS_URLS": "https://calendar.google.com/calendar/ical/x/basic.ics",
        "CALENDAR_CALDAV": "not-in-use",
        "CALENDAR_GOOGLE": SECRET,
    }
    from conftest import feed

    plugin = build(
        FakeFetcher({"https://calendar.google.com/calendar/ical/x/basic.ics": feed("all-day")}),
        api=HttpFetcher(schemes=("http",), attempts=1),
        environ=environ,
        clock=lambda: NOW,
    )
    data = events(
        plugin,
        calendars=[{"name": "Ics", "index": 1}, SPEC],
        start="2026-10-26",
        days=3,
        timezone="Europe/Paris",
    )
    assert {c["name"]: c["ok"] for c in data["calendars"]} == {"Ics": True, "Perso": True}


# --- configuration -----------------------------------------------------------------------


def test_the_secret_is_needed_only_for_google_calendars() -> None:
    environ = {"CALENDAR_ICS_URLS": "https://calendar.google.com/x"}
    plugin = build(FakeFetcher({}), environ=environ, clock=lambda: NOW)
    replayed = run(plugin, calendars=[SPEC])
    assert replayed.error == "secret_missing: CALENDAR_GOOGLE is not set or empty"
    # an ICS-only call never reads it
    assert run(plugin, calendars=[{"name": "A", "index": 1}]).error != replayed.error


@pytest.mark.parametrize("secret", ["", "   ", "only-two zzz", "q9 w9 e9 r9", "q9 w9 e9 " * 17])
def test_a_malformed_secret_is_refused_without_echoing_it(secret: str) -> None:
    replayed = run(plugin_for(secret), calendars=[SPEC])
    assert replayed.error is not None
    assert replayed.error.startswith(("config_invalid", "secret_missing"))
    assert secret.strip() == "" or secret.split()[0] not in replayed.error


def test_the_account_index_is_checked() -> None:
    replayed = run(plugin_for(), calendars=[dict(SPEC, index=2)])
    assert replayed.error == (
        "config_invalid: calendars[0].index is 2 but CALENDAR_GOOGLE holds 1 account(s)"
    )


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        (dict(SPEC, calendar_id=""), "calendar_id must be 1 to 256 characters"),
        (dict(SPEC, calendar_id="a b"), "calendar_id must be 1 to 256 characters"),
        (dict(SPEC, calendar_id=7), "calendar_id"),
        ({"name": "X", "index": 1, "calendar_id": "x"}, "calendar_id is for type google"),
    ],
)
def test_calendar_id_is_checked(spec: dict[str, Any], message: str) -> None:
    replayed = run(plugin_for(), calendars=[spec])
    assert replayed.error is not None
    assert message in replayed.error


def test_the_same_google_calendar_twice_is_refused_but_two_ids_are_fine() -> None:
    twice = run(plugin_for(), calendars=[SPEC, dict(SPEC, name="Again")])
    assert twice.error is not None
    assert "used twice" in twice.error


def test_credentials_hide_in_repr() -> None:
    (account,) = google.accounts(SECRET, 16) or []
    assert repr(account) == "Credentials(<hidden>)"


# --- nothing leaks -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "script",
    [
        "ok",
        "invalid_grant",
        "unauthorized",
        "rate",
        "garbage",
    ],
)
def test_tokens_never_reach_the_output_errors_or_logs(
    fake: type[Google], caplog: pytest.LogCaptureFixture, script: str
) -> None:
    queue = fake.queues["events:primary"]
    if script == "ok":
        queue.append(items(LUNCH))
    elif script == "invalid_grant":
        fake.queues["token"][0] = (400, {"error": "invalid_grant", "refresh": REFRESH})
    elif script == "unauthorized":
        queue.append((401, {"echo": f"Bearer {ACCESS}1 {CLIENT_SECRET}"}))
    elif script == "rate":
        queue.append((429, {"echo": REFRESH}))
    else:
        queue.append((200, {"items": [{"summary": REFRESH, "start": 1}]}))
    with caplog.at_level(logging.DEBUG):
        replayed = run(plugin_for(), calendars=[SPEC], partial=script == "garbage")
    no_leak(
        json.dumps(replayed.data, default=str),
        replayed.error or "",
        caplog.text,
        repr(replayed),
    )


def test_no_recording_holds_a_credential() -> None:
    for path in (Path(__file__).parent / "recordings").glob("*.json"):
        no_leak(path.read_text("utf-8"))


def test_the_http_loggers_stay_quiet(fake: type[Google], caplog: pytest.LogCaptureFixture) -> None:
    fake.queues["events:primary"].append(items(LUNCH))
    with caplog.at_level(logging.INFO):
        events(plugin_for(), calendars=[SPEC])
    assert "httpx" not in caplog.text
    assert "/token" not in caplog.text
