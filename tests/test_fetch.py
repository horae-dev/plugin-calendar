# SPDX-License-Identifier: Apache-2.0
"""`HttpFetcher` against real HTTP and HTTPS servers on 127.0.0.1 (never the network), and
`check_url`. No failure may reveal the URL, which holds the feed's token."""

import asyncio
import gzip
import logging
import ssl
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import trustme

from conftest import TOKEN, feed
from horae_calendar.fetch import FetchError, HttpFetcher, check_url

BODY = feed("weekly-exdate")
BIG = 64 * 1024


class Handler(BaseHTTPRequestHandler):
    hits: dict[str, int] = {}  # noqa: RUF012 (shared by the test server)

    def log_message(self, format: str, *args: object) -> None:
        """Silent: the test server must not write the token-bearing paths to stderr."""

    def send(self, status: int, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        if "Content-Length" not in (headers or {}):
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        route = path.rsplit("/", 1)[-1]
        Handler.hits[route] = Handler.hits.get(route, 0) + 1
        port = self.server.server_port  # type: ignore[attr-defined]
        if route == "ok.ics":
            self.send(200, BODY, {"Content-Type": "text/calendar"})
        elif route == "gzip.ics":
            self.send(200, gzip.compress(BODY), {"Content-Encoding": "gzip"})
        elif route == "bomb.ics":
            self.send(200, gzip.compress(b"\0" * (BIG * 4)), {"Content-Encoding": "gzip"})
        elif route == "bad-gzip.ics":
            self.send(200, b"not gzip at all", {"Content-Encoding": "gzip"})
        elif route == "big.ics":
            self.send(200, b"x" * (BIG + 1))
        elif route == "chunked-big.ics":
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for _ in range(8):
                chunk = b"x" * (BIG // 4)
                self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
        elif route == "slow.ics":
            time.sleep(1.5)
            self.send(200, BODY)
        elif route == "same-host":
            self.send(302, headers={"Location": f"/{TOKEN}/ok.ics?again=1"})
        elif route == "other-host":
            self.send(302, headers={"Location": f"{self.scheme()}://localhost:{port}/ok.ics"})
        elif route == "other-scheme":
            self.send(301, headers={"Location": f"ftp://127.0.0.1:{port}/ok.ics"})
        elif route == "no-location":
            self.send(302)
        elif route == "loop":
            self.send(307, headers={"Location": f"/{TOKEN}/loop"})
        elif route == "flaky.ics":
            if Handler.hits[route] % 2 == 1:
                self.send(503, b"try again")
            else:
                self.send(200, BODY)
        elif route == "forbidden.ics":
            self.send(403, f"denied {TOKEN}".encode())
        elif route == "busy.ics":
            self.send(429)
        else:
            self.send(404, b"nope")

    def scheme(self) -> str:
        return "https" if isinstance(self.connection, ssl.SSLSocket) else "http"


def serve(context: ssl.SSLContext | None = None) -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    if context is not None:
        server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    scheme = "https" if context else "http"
    try:
        yield f"{scheme}://127.0.0.1:{server.server_address[1]}/{TOKEN}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(scope="module")
def ca() -> trustme.CA:
    return trustme.CA()


@pytest.fixture(scope="module")
def https_base(ca: trustme.CA) -> Iterator[str]:
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ca.issue_cert("127.0.0.1", "localhost").configure_cert(context)
    yield from serve(context)


@pytest.fixture(scope="module")
def http_base() -> Iterator[str]:
    yield from serve()


@pytest.fixture
def client_context(ca: trustme.CA) -> ssl.SSLContext:
    context = ssl.create_default_context()
    ca.configure_trust(context)
    return context


def fetcher(context: ssl.SSLContext | None = None, **options: object) -> HttpFetcher:
    settings: dict[str, object] = {"max_bytes": BIG, "timeout_s": 3.0}
    settings.update(options)
    if context is None:
        return HttpFetcher(schemes=("http",), **settings)  # type: ignore[arg-type]
    return HttpFetcher(verify=context, **settings)  # type: ignore[arg-type]


def fetch(client: HttpFetcher, url: str) -> bytes:
    return asyncio.run(client.fetch(url))


def failure(client: HttpFetcher, url: str) -> FetchError:
    with pytest.raises(FetchError) as caught:
        fetch(client, url)
    error = caught.value
    assert TOKEN not in str(error)
    assert error.__cause__ is None  # no httpx error (which names the URL) chained
    assert error.__context__ is None or error.__suppress_context__
    return error


def test_https_fetch(https_base: str, client_context: ssl.SSLContext) -> None:
    assert fetch(fetcher(client_context), f"{https_base}/ok.ics") == BODY


def test_webcal_is_read_as_https(https_base: str, client_context: ssl.SSLContext) -> None:
    webcal = https_base.replace("https://", "webcal://")
    assert fetch(fetcher(client_context), f"{webcal}/ok.ics") == BODY


def test_https_with_an_untrusted_certificate(https_base: str) -> None:
    """The default trust store (certifi) does not know the test CA: TLS fails."""
    error = failure(HttpFetcher(timeout_s=3.0), f"{https_base}/ok.ics")
    assert error.reason == "tls_failed"


def test_plain_http_is_refused_by_default(http_base: str) -> None:
    assert failure(HttpFetcher(), f"{http_base}/ok.ics").reason == "not_https"


def test_gzip_is_decoded(http_base: str) -> None:
    assert fetch(fetcher(), f"{http_base}/gzip.ics") == BODY


def test_gzip_bomb_stops_at_the_cap(http_base: str) -> None:
    assert failure(fetcher(), f"{http_base}/bomb.ics").reason == "too_large"


def test_broken_gzip(http_base: str) -> None:
    assert failure(fetcher(), f"{http_base}/bad-gzip.ics").reason == "decode_failed"


@pytest.mark.parametrize("route", ["big.ics", "chunked-big.ics"])
def test_size_cap(http_base: str, route: str) -> None:
    assert failure(fetcher(), f"{http_base}/{route}").reason == "too_large"


def test_timeout(http_base: str) -> None:
    started = time.monotonic()
    error = failure(fetcher(timeout_s=0.5, attempts=2), f"{http_base}/slow.ics")
    assert error.reason == "timeout"
    assert time.monotonic() - started < 1.4  # the total deadline holds across retries


def test_same_host_redirect_is_followed(http_base: str) -> None:
    assert fetch(fetcher(), f"{http_base}/same-host") == BODY


@pytest.mark.parametrize("route", ["other-host", "other-scheme", "no-location"])
def test_other_redirects_are_refused(http_base: str, route: str) -> None:
    assert failure(fetcher(), f"{http_base}/{route}").reason == "redirect_refused"


def test_https_redirect_to_another_host_is_refused(
    https_base: str, client_context: ssl.SSLContext
) -> None:
    error = failure(fetcher(client_context), f"{https_base}/other-host")
    assert error.reason == "redirect_refused"


def test_redirect_loop(http_base: str) -> None:
    assert failure(fetcher(), f"{http_base}/loop").reason == "too_many_redirects"


@pytest.mark.parametrize(
    ("route", "reason"),
    [("missing.ics", "http_404"), ("forbidden.ics", "http_403"), ("busy.ics", "http_429")],
)
def test_non_200(http_base: str, route: str, reason: str) -> None:
    Handler.hits.clear()
    assert failure(fetcher(), f"{http_base}/{route}").reason == reason
    assert Handler.hits[route] == 1  # a 4xx is never retried


def test_one_retry_after_a_503(http_base: str) -> None:
    Handler.hits.clear()
    assert fetch(fetcher(), f"{http_base}/flaky.ics") == BODY
    assert Handler.hits["flaky.ics"] == 2


def test_no_retry_when_attempts_is_one(http_base: str) -> None:
    Handler.hits.clear()
    assert failure(fetcher(attempts=1), f"{http_base}/flaky.ics").reason == "http_503"


def test_connection_refused() -> None:
    error = failure(fetcher(), f"http://127.0.0.1:9/{TOKEN}/ok.ics")
    assert error.reason == "connect_failed"


def test_environment_proxies_are_ignored(http_base: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """trust_env=False: a proxy in the environment would see the URL; it is never used."""
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    assert fetch(fetcher(), f"{http_base}/ok.ics") == BODY


def test_nothing_logs_the_url(
    http_base: str, caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture[str]
) -> None:
    caplog.set_level(logging.DEBUG)
    fetch(fetcher(), f"{http_base}/ok.ics")
    failure(fetcher(), f"{http_base}/forbidden.ics")
    failure(fetcher(), f"{http_base}/other-host")
    assert TOKEN not in caplog.text
    out, err = capfd.readouterr()
    assert TOKEN not in out + err
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING


HOSTS = {"calendar.google.com", "calendar.proton.me"}


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://calendar.google.com/calendar/ical/x/private-y/basic.ics",
            "https://calendar.google.com/calendar/ical/x/private-y/basic.ics",
        ),
        (
            "webcal://Calendar.Proton.ME/api/calendar/v1/url/a/calendar.ics?CacheKey=b#frag",
            "https://calendar.proton.me/api/calendar/v1/url/a/calendar.ics?CacheKey=b",
        ),
        ("https://calendar.google.com:443/x", "https://calendar.google.com/x"),
        ("https://calendar.google.com", "https://calendar.google.com/"),
    ],
)
def test_check_url_accepts(url: str, expected: str) -> None:
    assert check_url(url, HOSTS) == expected


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://calendar.google.com/x", "not_https"),
        ("file:///etc/passwd", "not_https"),
        ("https://evil.example.org/x", "host_not_allowed"),
        ("https://calendar.google.com.evil.example.org/x", "host_not_allowed"),
        ("https://127.0.0.1/x", "host_not_allowed"),
        ("https://user:pw@calendar.google.com/x", "invalid_url"),
        ("https://calendar.google.com@evil.example.org/x", "invalid_url"),
        ("https://calendar.google.com:8443/x", "port_not_allowed"),
        ("https://calendar.google.com:99999/x", "invalid_url"),
        ("https://calendar.google.com/a b", "invalid_url"),
        ("https://calendar.google.com/a\\b", "invalid_url"),
        ("https://calendar.google.com/\x00", "invalid_url"),
        ("https:///x", "invalid_url"),
        ("", "invalid_url"),
        ("https://calendar.google.com/" + "x" * 5000, "invalid_url"),
        ("https://[::1/x", "invalid_url"),
        ("https://cälendar.google.com/x", "invalid_url"),
    ],
)
def test_check_url_refuses(url: str, reason: str) -> None:
    with pytest.raises(FetchError) as caught:
        check_url(url, HOSTS)
    assert caught.value.reason == reason
    assert str(caught.value) == reason
