# SPDX-License-Identifier: Apache-2.0
"""Fetch an ICS feed over HTTPS, bounded, without ever revealing its URL.

**The URL is a secret.** A Proton share link and a Google "secret address" carry the token
that grants read access in their path or query, so the URL is handled like a password: it is
read from a declared secret at call time, never logged, never put in an error, in returned
data or in a recording. Every failure is a `FetchError` with a fixed `reason` code
(`timeout`, `http_404`, `too_large`...), raised `from None` so no exception chain carries an
`httpx` message (which would hold the URL). The `httpx` and `httpcore` loggers are held at
`WARNING`: at `INFO`, `httpx` logs every request line with its full URL.

**Bounds.**

- `https` only (`webcal://`, what calendar apps show, is read as `https://`); no user info;
  the default port only; the host must be one of the allowed hosts (`check_url`);
- `trust_env=False`: no proxy, `.netrc` or CA bundle from the environment;
- a timeout for connecting and for each read, and a total deadline for the whole fetch,
  retries included;
- at most `max_bytes` (10 MiB by default) **after** decompression, so a gzip bomb stops at
  the cap; a larger `Content-Length` is refused before reading;
- **redirects: only to the same scheme, host and port, at most 3.** A share link that is
  moved within its service (a path normalised, a new feed path) still works, while a
  redirect can never send the token-bearing request, or the user's trust, to another host:
  the allow-list holds for every hop. Any other redirect fails with `redirect_refused`;
- one retry, at once, after a timeout, a connection error or a 500, 502, 503 or 504; never
  after 429 or another 4xx.
"""

import asyncio
import logging
import ssl
import time
from collections.abc import Collection
from typing import NamedTuple, Protocol
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

MAX_BYTES = 10 * 1024 * 1024
TIMEOUT_S = 20.0
CONNECT_TIMEOUT_S = 5.0
MAX_REDIRECTS = 3
MAX_URL = 4096
USER_AGENT = "horae-plugin-calendar"
_REDIRECTS = {301, 302, 303, 307, 308}
_RETRY_STATUS = {500, 502, 503, 504}
_RETRY_REASONS = {"timeout", "connect_failed", "transport_failed"}

for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)


class FetchError(Exception):
    """A feed that could not be fetched; `reason` is a fixed code, never the URL."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Fetcher(Protocol):
    """Where the feeds come from (`HttpFetcher`; the tests give a fake one)."""

    async def fetch(self, url: str) -> bytes:
        """The body at `url`, an already checked URL (`FetchError` otherwise)."""
        ...


def check_url(url: str, hosts: Collection[str]) -> str:
    """`url` normalised (`webcal` read as `https`, fragment dropped) when it is an `https`
    URL to one of `hosts` on the default port, with no user info; `FetchError` otherwise
    (`invalid_url`, `not_https`, `host_not_allowed`)."""
    parts = _split(url, schemes=("https",))
    if parts.hostname not in hosts:
        raise FetchError("host_not_allowed")
    if parts.port not in (None, 443):
        raise FetchError("port_not_allowed")
    return urlunsplit(("https", parts.hostname, parts.path or "/", parts.query, ""))


def _split(url: str, *, schemes: Collection[str]) -> "_Parts":
    if not url or len(url) > MAX_URL:
        raise FetchError("invalid_url")
    if any(ord(char) <= 0x20 or ord(char) == 0x7F or char == "\\" for char in url):
        raise FetchError("invalid_url")
    try:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        if scheme == "webcal":
            scheme = "https"
        port = parts.port
        host = parts.hostname
    except ValueError:
        raise FetchError("invalid_url") from None
    if scheme not in schemes:
        raise FetchError("not_https")
    if "@" in parts.netloc or not host or not host.isascii():
        raise FetchError("invalid_url")
    return _Parts(scheme, host.lower(), port, parts.path, parts.query)


class _Parts(NamedTuple):
    scheme: str
    hostname: str
    port: int | None
    path: str
    query: str

    def origin(self) -> tuple[str, str, int]:
        return self.scheme, self.hostname, self.port or (443 if self.scheme == "https" else 80)

    def url(self) -> str:
        netloc = self.hostname if self.port is None else f"{self.hostname}:{self.port}"
        return urlunsplit((self.scheme, netloc, self.path or "/", self.query, ""))


class HttpFetcher:
    """Fetches feeds with `httpx` (see the module docstring for the bounds).

    `verify` and `schemes` exist for the tests (a local CA, a plain-HTTP server); the plugin
    uses the defaults: the `certifi` CA bundle and `https` only."""

    def __init__(
        self,
        *,
        max_bytes: int = MAX_BYTES,
        timeout_s: float = TIMEOUT_S,
        connect_timeout_s: float = CONNECT_TIMEOUT_S,
        max_redirects: int = MAX_REDIRECTS,
        attempts: int = 2,
        verify: ssl.SSLContext | bool = True,
        schemes: Collection[str] = ("https",),
    ) -> None:
        self.max_bytes = max_bytes
        self.timeout_s = timeout_s
        self.connect_timeout_s = connect_timeout_s
        self.max_redirects = max_redirects
        self.attempts = attempts
        self.verify = verify
        self.schemes = tuple(schemes)

    async def fetch(self, url: str) -> bytes:
        target = _split(url, schemes=self.schemes)
        deadline = time.monotonic() + self.timeout_s
        timeout = httpx.Timeout(self.timeout_s, connect=min(self.connect_timeout_s, self.timeout_s))
        try:
            async with (
                asyncio.timeout(self.timeout_s),
                httpx.AsyncClient(
                    trust_env=False,
                    follow_redirects=False,
                    verify=self.verify,
                    timeout=timeout,
                    headers={"Accept": "text/calendar, */*;q=0.5", "User-Agent": USER_AGENT},
                ) as client,
            ):
                for attempt in range(1, self.attempts + 1):
                    try:
                        return await self._get(client, target)
                    except FetchError as error:
                        retry = error.reason in _RETRY_REASONS or error.reason in {
                            f"http_{status}" for status in _RETRY_STATUS
                        }
                        if not retry or attempt == self.attempts or time.monotonic() > deadline:
                            raise
        except FetchError:
            raise
        except TimeoutError:
            raise FetchError("timeout") from None
        except Exception:
            raise FetchError("transport_failed") from None
        raise FetchError("transport_failed")  # pragma: no cover (attempts >= 1)

    async def _get(self, client: httpx.AsyncClient, target: _Parts) -> bytes:
        current = target
        for _hop in range(self.max_redirects + 1):
            try:
                async with client.stream("GET", current.url()) as response:
                    if response.status_code in _REDIRECTS:
                        current = self._redirect(current, response.headers.get("location"))
                        continue
                    if response.status_code != 200:
                        raise FetchError(f"http_{response.status_code}")
                    return await self._body(response)
            except FetchError:
                raise
            except httpx.TimeoutException:
                raise FetchError("timeout") from None
            except httpx.DecodingError:
                raise FetchError("decode_failed") from None
            except httpx.ConnectError as error:
                raise FetchError(_connect_reason(error)) from None
            except (httpx.HTTPError, httpx.InvalidURL, ssl.SSLError, OSError):
                raise FetchError("transport_failed") from None
        raise FetchError("too_many_redirects")

    def _redirect(self, current: _Parts, location: str | None) -> _Parts:
        if not location:
            raise FetchError("redirect_refused")
        try:
            following = _split(urljoin(current.url(), location), schemes=self.schemes)
        except FetchError:
            raise FetchError("redirect_refused") from None
        if following.origin() != current.origin():
            raise FetchError("redirect_refused")
        return following

    async def _body(self, response: httpx.Response) -> bytes:
        declared = response.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_bytes:
            raise FetchError("too_large")
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > self.max_bytes:
                raise FetchError("too_large")
            chunks.append(chunk)
        return b"".join(chunks)


def _connect_reason(error: BaseException) -> str:
    """`tls_failed` when the connection failed on TLS (a certificate, a handshake)."""
    seen: BaseException | None = error
    for _ in range(8):
        if seen is None:
            break
        if isinstance(seen, ssl.SSLError):
            return "tls_failed"
        seen = seen.__cause__ or seen.__context__
    return "connect_failed"
