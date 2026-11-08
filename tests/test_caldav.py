# SPDX-License-Identifier: Apache-2.0
"""CalDAV: the `calendar-query`, the hardened multistatus parser, the merge of resources, the
`REPORT` client (Basic credentials, redirects only to allowed hosts), and `calendar.events`
with `type: caldav` giving exactly what the ICS path gives. Never the network."""

import asyncio
import base64
import threading
from collections.abc import Callable, Collection, Iterator
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import anyio
import pytest
from horae_sdk import Plugin
from horae_sdk.testing import call

from conftest import NOW, TOKEN, FakeFetcher, feed, window
from horae_calendar import build, caldav
from horae_calendar.fetch import FetchError, HttpFetcher
from horae_calendar.ics import expand

PASSWORD = "abcd-efgh-ijkl-mnop"  # noqa: S105 (synthetic)
USER = "user@example.org"
COLLECTION = f"https://caldav.icloud.com/123/calendars/{TOKEN}/"
SHARD = "p12-caldav.icloud.com"


def multistatus(*resources: bytes, extra: str = "") -> bytes:
    responses = "".join(
        f"<D:response><D:href>/c/{n}.ics</D:href><D:propstat><D:prop>"
        f"<C:calendar-data>{escape(resource.decode())}</C:calendar-data>"
        "</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>"
        for n, resource in enumerate(resources)
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
        f"{responses}{extra}</D:multistatus>"
    ).encode()


class FakeReporter(FakeFetcher):
    """Answers a `REPORT` from `answer` (bytes, or a `FetchError` reason) and records it."""

    def __init__(self, answer: bytes | str) -> None:
        super().__init__({})
        self.answer = answer
        self.reports: list[dict[str, Any]] = []

    async def report(
        self,
        url: str,
        body: bytes,
        *,
        user: str,
        password: str,
        follow: Callable[[str], bool] = lambda host: False,
    ) -> bytes:
        self.reports.append(
            {"url": url, "body": body, "user": user, "password": password, "follow": follow}
        )
        if isinstance(self.answer, str):
            raise FetchError(self.answer)
        return self.answer


def caldav_plugin(reporter: FakeReporter, secret: str | None = None) -> Plugin:
    environ = {
        "CALENDAR_ICS_URLS": "https://calendar.google.com/x",
        "CALENDAR_CALDAV": secret or f"{COLLECTION} {USER} {PASSWORD}",
    }
    return build(FakeFetcher({}), reporter=reporter, environ=environ, clock=lambda: NOW)


def run(plugin: Plugin, **params: Any) -> Any:
    async def go() -> Any:
        return await call(plugin, "calendar.events", params)

    return anyio.run(go)


def events(plugin: Plugin, **params: Any) -> dict[str, Any]:
    replayed = run(plugin, **params)
    assert replayed.error is None, replayed.error
    assert isinstance(replayed.data, dict)
    return replayed.data


SPEC = {"name": "iCloud", "index": 1, "type": "caldav"}


# --- the query, the accounts -------------------------------------------------------------


def test_query_is_a_time_range_calendar_query() -> None:
    body = caldav.query(window("2026-10-25", 2)).decode()
    assert "<C:calendar-query" in body
    assert 'name="VEVENT"' in body
    # 2026-10-25 00:00 +02:00 minus a day, and 2026-10-27 00:00 +01:00 plus a day, in UTC.
    assert 'time-range start="20261023T220000Z" end="20261027T230000Z"' in body


def test_accounts_are_whitespace_separated_triples() -> None:
    found = caldav.accounts("https://a/ u1 p1\nhttps://b/ u2 p2\n", 16)
    assert found is not None
    assert [(a.url, a.user, a.password) for a in found] == [
        ("https://a/", "u1", "p1"),
        ("https://b/", "u2", "p2"),
    ]
    assert "p1" not in repr(found[0])


@pytest.mark.parametrize("secret", ["", "   ", "not-in-use", "https://a/ u1", "a b c d"])
def test_accounts_that_are_not_triples(secret: str) -> None:
    assert caldav.accounts(secret, 16) is None


def test_too_many_accounts() -> None:
    assert caldav.accounts("a b c " * 17, 16) is None


# --- the hardened parser -----------------------------------------------------------------


def test_parse_returns_each_calendar_data() -> None:
    data = caldav.parse_multistatus(multistatus(feed("weekly-exdate"), feed("all-day")))
    assert len(data) == 2
    assert data[0].startswith("BEGIN:VCALENDAR")
    assert "Team standup" in data[0]


def test_parse_ignores_resources_without_data() -> None:
    extra = (
        "<D:response><D:href>/x</D:href><D:status>HTTP/1.1 404 Not Found</D:status></D:response>"
    )
    assert caldav.parse_multistatus(multistatus(extra=extra)) == []


def test_other_namespace_is_not_calendar_data() -> None:
    xml = (
        b'<D:multistatus xmlns:D="DAV:" xmlns:X="urn:other"><D:response>'
        b"<X:calendar-data>BEGIN:VCALENDAR</X:calendar-data></D:response></D:multistatus>"
    )
    assert caldav.parse_multistatus(xml) == []


LAUGHS = (
    b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY a "aaaaaaaaaa">'
    b'<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]>'
    b'<D:multistatus xmlns:D="DAV:">&b;</D:multistatus>'
)
XXE = (
    b'<?xml version="1.0"?><!DOCTYPE m [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
    b'<D:multistatus xmlns:D="DAV:">&x;</D:multistatus>'
)
DOCTYPE_ONLY = b'<!DOCTYPE m SYSTEM "http://127.0.0.1/evil.dtd"><D:multistatus xmlns:D="DAV:"/>'
PI = b'<?xml-stylesheet href="x"?><D:multistatus xmlns:D="DAV:"/>'
DEEP = b'<D:multistatus xmlns:D="DAV:">' + b"<D:a>" * 40 + b"</D:a>" * 40 + b"</D:multistatus>"


@pytest.mark.parametrize("xml", [LAUGHS, XXE, DOCTYPE_ONLY, PI, DEEP])
def test_hostile_xml_is_refused(xml: bytes) -> None:
    with pytest.raises(caldav.CalDavError) as caught:
        caldav.parse_multistatus(xml)
    assert caught.value.reason == "xml_refused"


@pytest.mark.parametrize(
    ("xml", "reason"),
    [
        (b"", "xml_refused"),
        (b"<html><body>login</body></html>", "not_multistatus"),
        (b'<D:other xmlns:D="DAV:"/>', "not_multistatus"),
        (b"not xml at all", "xml_refused"),
        (b'<D:multistatus xmlns:D="DAV:"><D:a></D:multistatus>', "xml_refused"),
    ],
)
def test_not_a_multistatus(xml: bytes, reason: str) -> None:
    with pytest.raises(caldav.CalDavError) as caught:
        caldav.parse_multistatus(xml)
    assert caught.value.reason == reason


def test_one_resource_over_the_size_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(caldav, "MAX_DATA", 100)
    with pytest.raises(caldav.CalDavError, match="xml_refused"):
        caldav.parse_multistatus(multistatus(feed("weekly-exdate")))


def test_too_many_resources(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(caldav, "MAX_RESOURCES", 1)
    with pytest.raises(caldav.CalDavError, match="xml_refused"):
        caldav.parse_multistatus(multistatus(feed("weekly-exdate"), feed("all-day")))


def test_too_many_elements(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(caldav, "MAX_ELEMENTS", 5)
    with pytest.raises(caldav.CalDavError, match="xml_refused"):
        caldav.parse_multistatus(multistatus(feed("weekly-exdate")))


# --- merge ---------------------------------------------------------------------------------


def test_merge_keeps_one_timezone_and_counts_unreadable_resources() -> None:
    texts = [feed("weekly-exdate").decode(), feed("weekly-exdate").decode(), "<html/>", ""]
    merged, bad = caldav.merge(texts)
    assert bad == 2
    assert merged.count(b"BEGIN:VTIMEZONE") == 1
    assert merged.count(b"BEGIN:VEVENT") == 2


@pytest.mark.parametrize(
    ("name", "start", "days"),
    [
        ("weekly-exdate", "2026-10-15", 14),
        ("dst", "2026-03-25", 10),
        ("overrides", "2026-10-15", 21),
        ("all-day", "2026-10-20", 7),
    ],
)
def test_events_equal_the_ics_path(name: str, start: str, days: int) -> None:
    """The same fixture as one CalDAV resource expands exactly as the ICS feed does."""
    win = window(start, days)
    direct = expand(feed(name), "X", win)
    merged, bad = caldav.merge(caldav.parse_multistatus(multistatus(feed(name))))
    via = expand(merged, "X", win)
    assert bad == 0
    assert direct.events
    assert [e.to_json() for e in via.events] == [e.to_json() for e in direct.events]
    assert (via.skipped, via.truncated) == (direct.skipped, direct.truncated)


# --- the tool ------------------------------------------------------------------------------


def test_tool_reads_caldav_and_sends_credentials_to_the_reporter() -> None:
    reporter = FakeReporter(multistatus(feed("weekly-exdate")))
    data = events(caldav_plugin(reporter), calendars=[SPEC], start="2026-10-22", days=1)
    assert data["complete"] is True
    assert [e["title"] for e in data["events"]] == ["Team standup"]
    assert data["calendars"][0]["name"] == "iCloud"
    (report,) = reporter.reports
    assert report["url"] == COLLECTION
    assert (report["user"], report["password"]) == (USER, PASSWORD)
    follow = report["follow"]
    assert not follow(SHARD)  # the shard is not an allowed host unless the call lists it
    assert not follow("calendar.google.com")  # allowed globally, but not this account's
    assert b"time-range" in report["body"]


def test_ics_and_caldav_calendars_share_a_call_and_indexes_are_per_type() -> None:
    reporter = FakeReporter(multistatus(feed("weekly-exdate")))
    environ = {
        "CALENDAR_ICS_URLS": "https://calendar.google.com/x",
        "CALENDAR_CALDAV": f"{COLLECTION} {USER} {PASSWORD}",
    }
    fetcher = FakeFetcher({"https://calendar.google.com/x": feed("all-day")})
    plugin = build(fetcher, reporter=reporter, environ=environ, clock=lambda: NOW)
    both = [{"name": "Ics", "index": 1}, SPEC]
    data = events(plugin, calendars=both, start="2026-10-22", days=2)
    assert [c["name"] for c in data["calendars"]] == ["Ics", "iCloud"]
    assert fetcher.fetched == ["https://calendar.google.com/x"]


def test_caldav_only_does_not_need_the_ics_secret() -> None:
    reporter = FakeReporter(multistatus(feed("weekly-exdate")))
    environ = {"CALENDAR_CALDAV": f"{COLLECTION} {USER} {PASSWORD}"}
    plugin = build(FakeFetcher({}), reporter=reporter, environ=environ, clock=lambda: NOW)
    assert events(plugin, calendars=[SPEC], start="2026-10-22", days=1)["complete"] is True


def test_ics_only_does_not_need_the_caldav_secret() -> None:
    plugin = build(
        FakeFetcher({"https://calendar.google.com/x": feed("all-day")}),
        environ={"CALENDAR_ICS_URLS": "https://calendar.google.com/x", "CALENDAR_CALDAV": "n/a"},
        clock=lambda: NOW,
    )
    assert events(plugin, start="2026-10-22", days=1)["window"]["days"] == 1


def test_secret_checks_never_echo_the_value() -> None:
    reporter = FakeReporter(b"")
    missing = run(caldav_plugin(reporter, " "), calendars=[SPEC]).error
    assert missing == "secret_missing: CALENDAR_CALDAV is not set or empty"
    bad = run(caldav_plugin(reporter, f"{COLLECTION} {USER}"), calendars=[SPEC]).error
    assert bad is not None
    assert bad.startswith("config_invalid: CALENDAR_CALDAV must hold")
    assert USER not in bad
    assert TOKEN not in bad
    far = run(caldav_plugin(reporter), calendars=[{**SPEC, "index": 2}]).error
    assert far == "config_invalid: calendars[0].index is 2 but CALENDAR_CALDAV holds 1 account(s)"


def test_the_same_index_in_two_types_is_allowed_but_not_twice() -> None:
    plugin = caldav_plugin(FakeReporter(b""))
    twice = [SPEC, {**SPEC, "name": "Again"}]
    assert "index is used twice" in str(run(plugin, calendars=twice).error)


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        ("http_401", "http_401"),
        ("redirect_refused", "redirect_refused"),
        (b"<html>sign in</html>", "not_multistatus"),
        (LAUGHS, "xml_refused"),
        (multistatus(b"BEGIN:VCALENDAR\r\nEND:VCALENDAR"), "complete"),
    ],
)
def test_failures_are_fixed_codes(answer: bytes | str, reason: str) -> None:
    plugin = caldav_plugin(FakeReporter(answer))
    replayed = run(plugin, calendars=[SPEC], start="2026-10-22", days=1)
    if reason == "complete":
        assert replayed.error is None  # a resource with no event: empty but complete
        assert replayed.data["events"] == []
        return
    assert replayed.error == f"calendar failed: iCloud: {reason}"
    assert PASSWORD not in replayed.error
    assert TOKEN not in replayed.error


def test_an_unreadable_resource_makes_the_calendar_incomplete() -> None:
    reporter = FakeReporter(multistatus(feed("weekly-exdate"), b"<html>junk</html>"))
    data = events(caldav_plugin(reporter), calendars=[SPEC], start="2026-10-22", days=1)
    assert data["complete"] is False
    assert data["calendars"][0]["skipped"] == 1
    assert len(data["events"]) == 1


def test_host_not_allowed_never_reaches_the_reporter() -> None:
    reporter = FakeReporter(b"")
    secret = f"https://evil.example.net/c/ {USER} {PASSWORD}"
    replayed = run(caldav_plugin(reporter, secret), calendars=[SPEC])
    assert replayed.error == "calendar failed: iCloud: host_not_allowed"
    assert reporter.reports == []


def test_partial_with_a_failing_caldav_calendar() -> None:
    reporter = FakeReporter("http_401")
    plugin = build(
        FakeFetcher({"https://calendar.google.com/x": feed("all-day")}),
        reporter=reporter,
        environ={
            "CALENDAR_ICS_URLS": "https://calendar.google.com/x",
            "CALENDAR_CALDAV": f"{COLLECTION} {USER} {PASSWORD}",
        },
        clock=lambda: NOW,
    )
    out = events(plugin, calendars=[{"name": "Ics", "index": 1}, SPEC], partial=True)
    assert out["complete"] is False
    assert out["calendars"][1] == {"name": "iCloud", "ok": False, "error": "http_401"}


# --- the REPORT client, against a local server ---------------------------------------------


class Server(BaseHTTPRequestHandler):
    seen: list[dict[str, Any]] = []  # noqa: RUF012 (shared by the test server)

    def log_message(self, format: str, *args: object) -> None:
        """Silent: the server must not write credentials or paths to stderr."""

    def do_REPORT(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        Server.seen.append(
            {
                "path": self.path,
                "host": self.headers.get("Host", ""),
                "auth": self.headers.get("Authorization"),
                "depth": self.headers.get("Depth"),
                "body": body,
            }
        )
        port = self.server.server_port  # type: ignore[attr-defined]
        route = self.path.rsplit("/", 1)[-1]
        if route == "ok":
            self.reply(207, multistatus(feed("weekly-exdate")))
        elif route == "unauthorized":
            self.reply(401, b"no")
        elif route == "wrong-status":
            self.reply(200, multistatus())
        elif route == "to-shard":
            self.reply(307, headers={"Location": f"http://localhost:{port}/shard/ok"})
        elif route == "to-stranger":
            self.reply(307, headers={"Location": f"http://127.0.0.2:{port}/ok"})
        elif route == "to-other-port":
            self.reply(307, headers={"Location": f"http://localhost:{port + 1}/ok"})
        elif route == "to-https":
            self.reply(307, headers={"Location": f"https://localhost:{port}/ok"})
        else:
            self.reply(404, b"")

    def reply(self, status: int, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def base() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Server)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/cal"
    server.shutdown()
    server.server_close()


def report(base: str, route: str, follow: Callable[[str], bool] = lambda host: False) -> bytes:
    client = HttpFetcher(schemes=("http",), timeout_s=3.0, attempts=1)
    return asyncio.run(
        client.report(f"{base}/{route}", b"<q/>", user=USER, password=PASSWORD, follow=follow)
    )


def test_report_sends_basic_credentials_depth_and_body(base: str) -> None:
    Server.seen.clear()
    assert b"multistatus" in report(base, "ok")
    (seen,) = Server.seen
    token = base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
    assert seen["auth"] == f"Basic {token}"
    assert seen["depth"] == "1"
    assert seen["body"] == b"<q/>"


def test_report_wants_a_207(base: str) -> None:
    with pytest.raises(FetchError) as caught:
        report(base, "wrong-status")
    assert caught.value.reason == "http_200"


def test_report_401_is_a_fixed_reason(base: str) -> None:
    with pytest.raises(FetchError) as caught:
        report(base, "unauthorized")
    assert caught.value.reason == "http_401"
    assert PASSWORD not in str(caught.value)


def test_report_follows_a_redirect_to_an_allowed_host_with_method_body_and_credentials(
    base: str,
) -> None:
    Server.seen.clear()
    assert b"multistatus" in report(base, "to-shard", follow=lambda host: host == "localhost")
    assert [s["path"] for s in Server.seen] == ["/cal/to-shard", "/shard/ok"]
    assert Server.seen[1]["auth"] == Server.seen[0]["auth"]
    assert Server.seen[1]["body"] == b"<q/>"


@pytest.mark.parametrize("route", ["to-shard", "to-stranger", "to-other-port", "to-https"])
def test_report_refuses_a_redirect_elsewhere(base: str, route: str) -> None:
    Server.seen.clear()
    with pytest.raises(FetchError) as caught:
        report(base, route)  # same origin only: nothing else is followed
    assert caught.value.reason == "redirect_refused"
    assert len(Server.seen) == 1  # the credentials went nowhere else


def test_report_does_not_follow_to_an_unlisted_port_even_on_an_allowed_host(base: str) -> None:
    with pytest.raises(FetchError) as caught:
        report(base, "to-other-port", follow=lambda host: host == "localhost")
    assert caught.value.reason == "redirect_refused"


def test_report_over_plain_http_is_refused_by_default(base: str) -> None:
    client = HttpFetcher()
    with pytest.raises(FetchError) as caught:
        asyncio.run(
            client.report(f"{base}/ok", b"", user=USER, password=PASSWORD, follow=lambda host: True)
        )
    assert caught.value.reason == "not_https"


# --- credentials follow a redirect only to their account's shard -----------------------------

ALLOWED = {"caldav.icloud.com", SHARD, "calendar.google.com", "evil.example.net"}


def test_icloud_account_follows_only_a_strict_allowed_shard() -> None:
    follow = caldav.shard_follow("caldav.icloud.com", ALLOWED)
    assert follow(SHARD)
    assert follow("p7-caldav.icloud.com") is False  # a shard, but not approved
    for host in (
        "calendar.google.com",  # allowed globally, not this account's
        "evil.example.net",
        "caldav.icloud.com",
        "p1234-caldav.icloud.com",
        "evil-caldav.icloud.com",
        "p12-caldav.icloud.com.evil.net",
        "xp12-caldav.icloud.com",
        "p12-caldav-icloud.com",
        "P12-caldav.icloud.com",
    ):
        assert follow(host) is False, host


def test_a_shard_account_follows_a_shard_too() -> None:
    assert caldav.shard_follow(SHARD, ALLOWED | {"p13-caldav.icloud.com"})("p13-caldav.icloud.com")


@pytest.mark.parametrize("host", ["calendar.google.com", "dav.example.net"])
def test_other_accounts_follow_nothing_even_an_allowed_shard(host: str) -> None:
    follow = caldav.shard_follow(host, ALLOWED)
    assert not any(follow(other) for other in (*ALLOWED, "p12-caldav.icloud.com"))


def test_the_tool_binds_follow_to_the_account_and_allowed_hosts() -> None:
    reporter = FakeReporter(multistatus())
    plugin = caldav_plugin(reporter)
    events(plugin, calendars=[SPEC], allowed_hosts=[SHARD, "evil.example.net"])
    follow = reporter.reports[0]["follow"]
    assert follow(SHARD)
    assert not follow("evil.example.net")
    assert not follow("calendar.google.com")
    other = caldav_plugin(reporter, f"https://calendar.google.com/dav/ {USER} {PASSWORD}")
    events(other, calendars=[SPEC], allowed_hosts=[SHARD])
    assert not reporter.reports[1]["follow"](SHARD)
